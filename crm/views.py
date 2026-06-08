import base64
import json
import logging
import requests
from datetime import date
from functools import wraps
from io import BytesIO

logger = logging.getLogger(__name__)

from django.conf import settings as django_settings
from django.contrib.auth import login, logout, get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
import datetime
import re
from django.db.models import Avg, Sum, Count, Case, When, IntegerField, Value, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_http_methods, require_POST

from .models import (STAGE_META, HEAT_META, CALL_OUTCOME_CHOICES, Contact, Company, Opportunity, TouchPoint,
                     HeatSettings, calculate_score, auto_heat, InvitedEmail,
                     UserProfile, Workspace, WorkspaceMembership,
                     EmailThread, AICallLog, EmailTemplate, EmailImage,
                     OutreachAttachment, EmailTemplateAttachment, TaskJob, SavedFilter)

MASTER_EMAIL   = django_settings.MASTER_EMAIL
INBOUND_DOMAIN = 'nareosa.resend.app'


def _api_login_required(view_func):
    """For API endpoints: return 401 JSON instead of a redirect."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        return view_func(request, *args, **kwargs)
    return wrapper


# ── Workspace helpers ──────────────────────────────────────────────────────────

def _get_workspace(request):
    """Resolve the active workspace. Returns (workspace, membership) or (None, None)."""
    is_master = (request.user.email == MASTER_EMAIL)
    ws_id = request.session.get('active_workspace_id')

    if not ws_id:
        if is_master:
            ws = Workspace.objects.filter(owner=request.user).first()
            if ws:
                request.session['active_workspace_id'] = ws.pk
                membership = WorkspaceMembership.objects.filter(workspace=ws, user=request.user).first()
                return ws, membership
        return None, None

    try:
        ws = Workspace.objects.get(pk=ws_id)
    except Workspace.DoesNotExist:
        del request.session['active_workspace_id']
        return None, None

    if is_master:
        membership = WorkspaceMembership.objects.filter(workspace=ws, user=request.user).first()
        return ws, membership

    try:
        membership = WorkspaceMembership.objects.get(workspace=ws, user=request.user)
        return ws, membership
    except WorkspaceMembership.DoesNotExist:
        del request.session['active_workspace_id']
        return None, None


def workspace_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(django_settings.LOGIN_URL)
        workspace, membership = _get_workspace(request)
        if workspace is None:
            return redirect('workspace_select')
        kwargs['workspace']  = workspace
        kwargs['membership'] = membership
        return view_func(request, *args, **kwargs)
    return wrapper


def _api_workspace_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        workspace, membership = _get_workspace(request)
        if workspace is None:
            return JsonResponse({'error': 'No active workspace'}, status=403)
        kwargs['workspace']  = workspace
        kwargs['membership'] = membership
        return view_func(request, *args, **kwargs)
    return wrapper


def _is_admin(request, membership):
    return request.user.email == MASTER_EMAIL or (membership and membership.role in ('owner', 'admin'))


# ── Auth views ─────────────────────────────────────────────────────────────────

_LOGIN_SCOPES = ['openid', 'email', 'profile']


def login_page(request):
    if request.user.is_authenticated:
        return redirect('/')
    error = request.session.pop('login_error', None)
    return render(request, 'crm/login.html', {'error': error})


def google_login(request):
    from google_auth_oauthlib.flow import Flow

    client_id     = django_settings.GOOGLE_LOGIN_CLIENT_ID
    client_secret = django_settings.GOOGLE_LOGIN_CLIENT_SECRET
    if not client_id or not client_secret:
        request.session['login_error'] = 'Google login is not configured — contact the administrator.'
        return redirect('/auth/login/')

    callback_uri = django_settings.SITE_URL.rstrip('/') + '/auth/callback/'
    flow = Flow.from_client_config(
        {'web': {
            'client_id':     client_id,
            'client_secret': client_secret,
            'auth_uri':      'https://accounts.google.com/o/oauth2/auth',
            'token_uri':     'https://oauth2.googleapis.com/token',
            'redirect_uris': [callback_uri],
        }},
        scopes=_LOGIN_SCOPES,
    )
    flow.redirect_uri = callback_uri
    auth_url, state = flow.authorization_url(access_type='offline', prompt='select_account')
    request.session['google_login_state'] = state
    if flow.code_verifier:
        request.session['google_code_verifier'] = flow.code_verifier
    return redirect(auth_url)


def google_callback(request):
    import os
    from google_auth_oauthlib.flow import Flow

    if django_settings.DEBUG:
        os.environ.setdefault('OAUTHLIB_INSECURE_TRANSPORT', '1')
    os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

    client_id     = django_settings.GOOGLE_LOGIN_CLIENT_ID
    client_secret = django_settings.GOOGLE_LOGIN_CLIENT_SECRET
    state         = request.session.get('google_login_state', '')
    callback_uri  = django_settings.SITE_URL.rstrip('/') + '/auth/callback/'

    try:
        flow = Flow.from_client_config(
            {'web': {
                'client_id':     client_id,
                'client_secret': client_secret,
                'auth_uri':      'https://accounts.google.com/o/oauth2/auth',
                'token_uri':     'https://oauth2.googleapis.com/token',
                'redirect_uris': [callback_uri],
            }},
            scopes=_LOGIN_SCOPES,
            state=state,
        )
        flow.redirect_uri = callback_uri
        code_verifier = request.session.get('google_code_verifier')
        fetch_kwargs = {'authorization_response': request.build_absolute_uri()}
        if code_verifier:
            fetch_kwargs['code_verifier'] = code_verifier
        flow.fetch_token(**fetch_kwargs)
        creds = flow.credentials

        resp      = requests.get('https://www.googleapis.com/oauth2/v2/userinfo',
                                 headers={'Authorization': f'Bearer {creds.token}'}, timeout=5)
        user_info = resp.json()
        email     = user_info.get('email', '').lower().strip()
        name      = user_info.get('name', '').strip()

        if not email:
            request.session['login_error'] = 'Could not retrieve your email from Google.'
            return redirect('/auth/login/')

        if email.lower() != MASTER_EMAIL.lower() and \
                not InvitedEmail.objects.filter(email__iexact=email).exists():
            request.session['login_error'] = "You haven't been invited to access this app."
            return redirect('/auth/login/')

        User = get_user_model()
        parts = name.split(' ', 1)
        user, created = User.objects.get_or_create(
            username=email,
            defaults={
                'email':      email,
                'first_name': parts[0] if parts else '',
                'last_name':  parts[1] if len(parts) > 1 else '',
            },
        )
        if not created and user.email != email:
            user.email = email
            user.save(update_fields=['email'])

        login(request, user, backend='django.contrib.auth.backends.ModelBackend')

        # Set active workspace
        if user.email == MASTER_EMAIL:
            if not request.session.get('active_workspace_id'):
                ws = Workspace.objects.filter(owner=user).first()
                if ws:
                    request.session['active_workspace_id'] = ws.pk
            return redirect('/')

        # For first-time logins: auto-create workspace membership from invite record
        if created:
            invite_record = InvitedEmail.objects.filter(email__iexact=email).first()
            if invite_record and invite_record.workspace:
                WorkspaceMembership.objects.get_or_create(
                    workspace=invite_record.workspace,
                    user=user,
                    defaults={'role': invite_record.role or 'member'},
                )

        memberships = WorkspaceMembership.objects.filter(user=user)
        if memberships.count() == 1:
            request.session['active_workspace_id'] = memberships.first().workspace_id
            return redirect('/')
        elif memberships.count() > 1:
            return redirect('workspace_select')
        else:
            request.session['login_error'] = 'You have not been added to any workspace yet — contact the administrator.'
            return redirect('/auth/login/')

    except Exception as e:
        request.session['login_error'] = f'Login failed: {e}'
        return redirect('/auth/login/')


@require_POST
def logout_view(request):
    logout(request)
    return redirect('/auth/login/')


# ── Workspace management ───────────────────────────────────────────────────────

@login_required
def workspace_select(request):
    is_master = (request.user.email == MASTER_EMAIL)
    if is_master:
        workspaces = Workspace.objects.all().order_by('name').select_related('owner').annotate(
            member_count=Count('memberships')
        )
    else:
        memberships = WorkspaceMembership.objects.filter(user=request.user).select_related('workspace__owner')
        workspaces = [m.workspace for m in memberships]

    if not is_master and len(workspaces) == 1:
        request.session['active_workspace_id'] = workspaces[0].pk
        return redirect('dashboard')

    return render(request, 'crm/workspace_select.html', {
        'workspaces': workspaces,
        'is_master':  is_master,
    })


@login_required
@require_POST
def workspace_switch(request):
    data  = request.POST or {}
    ws_id = data.get('workspace_id')
    if not ws_id:
        try:
            ws_id = json.loads(request.body).get('workspace_id')
        except Exception:
            pass
    try:
        ws = Workspace.objects.get(pk=int(ws_id))
    except (Workspace.DoesNotExist, TypeError, ValueError):
        return JsonResponse({'error': 'Workspace not found'}, status=404)

    is_master = (request.user.email == MASTER_EMAIL)
    if not is_master and not WorkspaceMembership.objects.filter(workspace=ws, user=request.user).exists():
        return JsonResponse({'error': 'Not a member of this workspace'}, status=403)

    request.session['active_workspace_id'] = ws.pk
    return redirect('dashboard')


def _parse_emails(raw):
    """Split a textarea of emails (comma/newline/space separated) into a clean list."""
    import re
    return [e.strip().lower() for e in re.split(r'[\s,;]+', raw) if e.strip() and '@' in e]


def _add_workspace_members(ws, emails, role):
    """Ensure each email is in InvitedEmail and has a WorkspaceMembership for ws."""
    User = get_user_model()
    for email in emails:
        InvitedEmail.objects.get_or_create(email=email)
        user_qs = User.objects.filter(username=email)
        if user_qs.exists():
            mem, created = WorkspaceMembership.objects.get_or_create(
                workspace=ws, user=user_qs.first(), defaults={'role': role}
            )
            if not created and mem.role != role:
                mem.role = role
                mem.save(update_fields=['role'])


@login_required
def _logo_to_dataurl(file_obj, max_px=256):
    from PIL import Image
    img = Image.open(file_obj)
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    if img.mode not in ('RGB', 'RGBA'):
        img = img.convert('RGBA')
    buf = BytesIO()
    img.save(buf, format='PNG', optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return f'data:image/png;base64,{b64}'


def workspace_create(request):
    if request.user.email != MASTER_EMAIL:
        raise Http404

    if request.method == 'POST':
        name         = request.POST.get('name', '').strip()
        logo         = request.FILES.get('logo')
        owner_email  = request.POST.get('owner_email', '').strip().lower()
        admin_emails = _parse_emails(request.POST.get('admin_emails', ''))
        member_emails = _parse_emails(request.POST.get('member_emails', ''))

        if not name:
            return render(request, 'crm/workspace_create.html', {'error': 'Name is required'})
        if not owner_email or '@' not in owner_email:
            return render(request, 'crm/workspace_create.html', {'error': 'Owner email is required'})

        # Get or create the owner user
        User  = get_user_model()
        owner, _ = User.objects.get_or_create(
            username=owner_email, defaults={'email': owner_email}
        )
        InvitedEmail.objects.get_or_create(email=owner_email)

        ws = Workspace.objects.create(
            name=name, owner=owner,
            logo=_logo_to_dataurl(logo) if logo else '',
        )
        WorkspaceMembership.objects.create(workspace=ws, user=owner, role='owner')

        # Always add master as owner-level member too
        if owner_email != MASTER_EMAIL:
            WorkspaceMembership.objects.get_or_create(
                workspace=ws, user=request.user, defaults={'role': 'owner'}
            )

        HeatSettings.objects.create(workspace=ws)
        _add_workspace_members(ws, admin_emails, 'admin')
        _add_workspace_members(ws, member_emails, 'member')

        request.session['active_workspace_id'] = ws.pk
        return redirect('dashboard')

    return render(request, 'crm/workspace_create.html')


@_api_workspace_required
@require_POST
def workspace_update_logo(request, workspace, membership):
    if not _is_admin(request, membership):
        return JsonResponse({'error': 'Insufficient permissions'}, status=403)
    logo_file = request.FILES.get('logo')
    if not logo_file:
        return JsonResponse({'error': 'No file provided'}, status=400)
    try:
        data_url = _logo_to_dataurl(logo_file)
        workspace.logo = data_url
        workspace.save(update_fields=['logo'])
        return JsonResponse({'ok': True, 'logo_url': data_url})
    except Exception:
        logger.exception('workspace_update_logo failed')
        return JsonResponse({'error': 'Failed to update logo'}, status=500)


@login_required
@require_POST
def workspace_delete(request, pk):
    try:
        ws = Workspace.objects.get(pk=pk)
    except Workspace.DoesNotExist:
        raise Http404

    is_master = (request.user.email == MASTER_EMAIL)
    if not is_master and ws.owner != request.user:
        return JsonResponse({'error': 'Only the workspace owner can delete it'}, status=403)

    if request.session.get('active_workspace_id') == ws.pk:
        del request.session['active_workspace_id']

    ws.delete()
    return redirect('workspace_select')


@_api_workspace_required
@require_POST
def workspace_invite(request, workspace, membership):
    if not _is_admin(request, membership):
        return JsonResponse({'error': 'Insufficient permissions'}, status=403)

    data  = json.loads(request.body)
    email = data.get('email', '').strip().lower()
    role  = data.get('role', 'member')
    if role not in ('admin', 'member'):
        role = 'member'
    if not email:
        return JsonResponse({'error': 'Email is required'}, status=400)

    invite, just_invited = InvitedEmail.objects.get_or_create(email=email)
    # Always keep the pending workspace/role up-to-date so that the
    # membership is created correctly when they log in for the first time.
    if invite.workspace != workspace or invite.role != role:
        invite.workspace = workspace
        invite.role = role
        invite.save(update_fields=['workspace', 'role'])

    User    = get_user_model()
    user_qs = User.objects.filter(username=email)
    if user_qs.exists():
        user = user_qs.first()
        mem, created = WorkspaceMembership.objects.get_or_create(
            workspace=workspace, user=user, defaults={'role': role}
        )
        if not created and mem.role != role:
            mem.role = role
            mem.save(update_fields=['role'])

    # Send invite email if this is a new invitation and Resend is configured
    if just_invited:
        try:
            import resend as _resend
            cfg = workspace.heat_settings
            if cfg.resend_api_key:
                _resend.api_key = cfg.resend_api_key
                login_url = django_settings.SITE_URL.rstrip('/') + '/auth/google/'
                from_addr = cfg.resend_from_email or 'WVVYphone <noreply@wvvy.pro>'
                _resend.Emails.send({
                    'from':    from_addr,
                    'to':      [email],
                    'subject': f"You've been invited to {workspace.name} on WVVYphone",
                    'html': f"""
<div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:#0f1117;color:#eeeef5;border-radius:12px;">
  <h2 style="margin:0 0 8px;font-size:1.25rem;color:#00e8c8;">You're invited to WVVYphone</h2>
  <p style="margin:0 0 24px;color:#9899b0;font-size:0.9375rem;">
    You've been added to <strong style="color:#eeeef5;">{workspace.name}</strong> as a {role}.
    Click the button below to sign in with your Google account.
  </p>
  <a href="{login_url}" style="display:inline-block;background:#00e8c8;color:#0f1117;font-weight:700;font-size:0.875rem;padding:12px 28px;border-radius:99px;text-decoration:none;">
    Sign in with Google
  </a>
  <p style="margin:24px 0 0;color:#5a5b72;font-size:0.75rem;">
    You're receiving this because {request.user.email} added you to WVVYphone.
  </p>
</div>""",
                })
        except Exception:
            pass  # Don't block the invite if email fails

    return JsonResponse({'ok': True, 'email': email})


@_api_workspace_required
@require_POST
def workspace_remove_member(request, workspace, membership):
    if not _is_admin(request, membership):
        return JsonResponse({'error': 'Insufficient permissions'}, status=403)

    data    = json.loads(request.body)
    user_id = data.get('user_id')

    if workspace.owner_id == int(user_id):
        return JsonResponse({'error': 'Cannot remove the workspace owner'}, status=400)

    WorkspaceMembership.objects.filter(workspace=workspace, user_id=user_id).delete()
    return JsonResponse({'ok': True})


# ── AI draft review queue ──────────────────────────────────────────────────────

@workspace_required
def ai_drafts(request, workspace, membership):
    """Page showing all pending AI drafts for review."""
    from .models import AICallLog
    drafts = (AICallLog.objects
              .filter(contact__workspace=workspace, status='pending')
              .select_related('contact')
              .order_by('created_at'))
    return render(request, 'crm/ai_drafts.html', {'drafts': drafts})


@_api_workspace_required
@require_POST
def approve_ai_draft(request, pk, workspace, membership):
    """Approve (optionally with edits) a pending AI draft and send it."""
    import resend as resend_sdk
    from .models import AICallLog

    try:
        call_log = AICallLog.objects.select_related('contact').get(pk=pk, status='pending')
    except AICallLog.DoesNotExist:
        return JsonResponse({'error': 'Draft not found or already actioned'}, status=404)

    if call_log.contact.workspace_id != workspace.pk:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    data          = json.loads(request.body)
    send_text     = data.get('text', '').strip() or call_log.response
    is_edited     = send_text != call_log.response

    contact   = call_log.contact
    cfg       = HeatSettings.get_for_workspace(workspace)
    owner_profile = UserProfile.get_for_user(workspace.owner)
    from_addr = cfg.resend_from_email or owner_profile.from_email or 'noreply@wvvy.pro'

    _do_send_ai_email(
        contact, send_text,
        call_log.draft_subject,
        call_log.draft_inbound_msg_id,
        call_log.draft_in_reply_to,
        '',   # inbound_in_reply_to not stored on drafts; threading covered by in_reply_to
        cfg, from_addr, resend_sdk, call_log,
    )

    # Only update status if _do_send_ai_email didn't flag an error
    call_log.refresh_from_db(fields=['flagged'])
    if not call_log.flagged:
        call_log.status = 'edited' if is_edited else 'approved'
        if is_edited:
            call_log.edited_response = send_text
        call_log.save(update_fields=['status', 'edited_response'])

    return JsonResponse({'ok': True})


@_api_workspace_required
@require_POST
def reject_ai_draft(request, pk, workspace, membership):
    """Discard a pending AI draft."""
    from .models import AICallLog

    try:
        call_log = AICallLog.objects.get(pk=pk, status='pending')
    except AICallLog.DoesNotExist:
        return JsonResponse({'error': 'Draft not found or already actioned'}, status=404)

    if call_log.contact.workspace_id != workspace.pk:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    call_log.status = 'rejected'
    call_log.save(update_fields=['status'])
    return JsonResponse({'ok': True})


@login_required
def master_panel(request):
    if request.user.email != MASTER_EMAIL:
        raise Http404

    from .models import AICallLog
    workspaces = Workspace.objects.annotate(
        member_count=Count('memberships')
    ).select_related('owner').order_by('name')

    ai_logs = AICallLog.objects.select_related('contact').order_by('-created_at')[:50]

    return render(request, 'crm/master_panel.html', {
        'workspaces':    workspaces,
        'active_ws_id':  request.session.get('active_workspace_id'),
        'ai_logs':       ai_logs,
    })


# ── Dashboard ──────────────────────────────────────────────────────────────────

@workspace_required
def dashboard(request, workspace, membership):
    from django.utils import timezone as tz
    contact_stages = []
    for key, label, badge in STAGE_META:
        c_qs = Contact.objects.filter(workspace=workspace, stage=key).order_by('-created_at')
        contact_stages.append({'key': key, 'label': label, 'badge': badge,
                                'records': c_qs[:10], 'count': c_qs.count()})

    all_contacts   = Contact.objects.filter(workspace=workspace)
    ws_contact_ids = all_contacts.values_list('id', flat=True)
    contact_ct     = ContentType.objects.get_for_model(Contact)
    now            = tz.now()

    # Time-range selector (affects outreach KPIs; pipeline totals are always all-time)
    time_range = request.GET.get('range', '30d')
    if time_range == '7d':
        range_days = 7
    elif time_range == '90d':
        range_days = 90
    else:
        time_range = '30d'
        range_days = 30
    range_start = now - datetime.timedelta(days=range_days)

    # Outreach KPIs (time-range aware)
    leads_reached = EmailThread.objects.filter(
        contact_id__in=ws_contact_ids, direction='outbound',
        sent_at__gte=range_start,
    ).values('contact_id').distinct().count()
    replies_received = EmailThread.objects.filter(
        contact_id__in=ws_contact_ids, direction='inbound',
        sent_at__gte=range_start,
    ).values('contact_id').distinct().count()
    reply_rate = round(replies_received / leads_reached * 100) if leads_reached else 0
    ai_replies_sent = AICallLog.objects.filter(
        contact_id__in=ws_contact_ids, flagged=False,
        created_at__gte=range_start,
    ).count()
    meetings_booked = TouchPoint.objects.filter(
        content_type=contact_ct, object_id__in=ws_contact_ids,
        touchpoint_type='meeting', created_at__gte=range_start,
    ).count()
    pending_drafts_count = AICallLog.objects.filter(
        contact_id__in=ws_contact_ids, status='pending',
    ).count()

    # Phone stats (all-time — called is a boolean field, not timestamped)
    calls_made    = all_contacts.filter(called=True).count()
    calls_pending = all_contacts.filter(called=False).exclude(
        stage__in=['closed_won', 'closed_lost']
    ).count()

    # ── Weekly chart data (last 7 weeks, fixed window) ─────────────────────────
    weekly_data = []
    for i in range(6, -1, -1):
        wk_end   = now - datetime.timedelta(weeks=i)
        wk_start = now - datetime.timedelta(weeks=i + 1)
        w_reached = EmailThread.objects.filter(
            contact_id__in=ws_contact_ids, direction='outbound',
            sent_at__gte=wk_start, sent_at__lt=wk_end,
        ).values('contact_id').distinct().count()
        w_replied = EmailThread.objects.filter(
            contact_id__in=ws_contact_ids, direction='inbound',
            sent_at__gte=wk_start, sent_at__lt=wk_end,
        ).values('contact_id').distinct().count()
        w_ai = AICallLog.objects.filter(
            contact_id__in=ws_contact_ids,
            created_at__gte=wk_start, created_at__lt=wk_end,
        ).count()
        w_calls = TouchPoint.objects.filter(
            content_type=contact_ct, object_id__in=ws_contact_ids,
            touchpoint_type='call',
            created_at__gte=wk_start, created_at__lt=wk_end,
        ).count()
        weekly_data.append({
            'week': f'W{7 - i}',
            'reached': w_reached,
            'replied': w_replied,
            'ai': w_ai,
            'calls': w_calls,
        })

    # ── AI activity donut ──────────────────────────────────────────────────────
    ai_auto     = AICallLog.objects.filter(contact_id__in=ws_contact_ids, status='auto_sent').count()
    ai_approved = AICallLog.objects.filter(contact_id__in=ws_contact_ids, status__in=['approved', 'edited']).count()
    ai_rejected = AICallLog.objects.filter(contact_id__in=ws_contact_ids, status='rejected').count()
    ai_donut    = {'auto': ai_auto, 'approved': ai_approved, 'manual': ai_rejected}

    # ── Activity feed (most-recent 6 from emails + touchpoints) ───────────────
    recent_emails = list(
        EmailThread.objects.filter(contact_id__in=ws_contact_ids)
        .select_related('contact').order_by('-sent_at')[:8]
    )
    recent_tps = list(
        TouchPoint.objects.filter(
            content_type=contact_ct, object_id__in=ws_contact_ids,
        ).order_by('-created_at')[:8]
    )
    tp_ids   = [tp.object_id for tp in recent_tps]
    name_map = dict(Contact.objects.filter(pk__in=tp_ids).values_list('pk', 'name')) if tp_ids else {}
    activity_feed = []
    for e in recent_emails:
        activity_feed.append({
            'type':  'email_out' if e.direction == 'outbound' else 'email_in',
            'name':  e.contact.name if e.contact else 'Unknown',
            'label': 'Outreach sent' if e.direction == 'outbound' else 'Reply received',
            'ts':    e.sent_at.isoformat(),
        })
    for tp in recent_tps:
        activity_feed.append({
            'type':  'touchpoint',
            'name':  name_map.get(tp.object_id, 'Unknown'),
            'label': tp.get_touchpoint_type_display(),
            'ts':    tp.created_at.isoformat(),
        })
    activity_feed.sort(key=lambda x: x['ts'], reverse=True)
    activity_feed = activity_feed[:6]

    # ── Geo distribution (top 4 locations) ────────────────────────────────────
    geo_data = list(
        Contact.objects.filter(workspace=workspace)
        .exclude(location='').exclude(location__isnull=True)
        .values('location')
        .annotate(count=Count('id'))
        .order_by('-count')[:4]
    )

    # ── Lead funnel (active pipeline stages only) ──────────────────────────────
    funnel_data = [
        {'label': label, 'count': all_contacts.filter(stage=key).count()}
        for key, label, _ in STAGE_META
        if key not in ('closed_won', 'closed_lost')
    ]

    from .models import INDUSTRY_LIST
    email_templates_json = json.dumps(
        list(workspace.email_templates.values('id', 'name', 'is_default'))
    )
    context = {
        'contact_stages':       contact_stages,
        'email_templates_json': email_templates_json,
        'contact_total':        all_contacts.count(),
        'contact_active':       all_contacts.filter(stage__in=['discovery_call', 'proposal', 'negotiation']).count(),
        'contact_won':          all_contacts.filter(stage='closed_won').count(),
        'leads_reached':        leads_reached,
        'replies_received':     replies_received,
        'reply_rate':           reply_rate,
        'ai_replies_sent':      ai_replies_sent,
        'calls_made':           calls_made,
        'calls_pending':        calls_pending,
        'pending_drafts_count': pending_drafts_count,
        'meetings_booked':      meetings_booked,
        'time_range':           time_range,
        'weekly_data_json':     json.dumps(weekly_data),
        'ai_donut_json':        json.dumps(ai_donut),
        'activity_feed_json':   json.dumps(activity_feed),
        'geo_data_json':        json.dumps(geo_data),
        'funnel_data_json':     json.dumps(funnel_data),
        'workspace':            workspace,
        'membership':           membership,
        'industry_list':        INDUSTRY_LIST,
        'source_choices':       Contact._meta.get_field('source').choices,
        'stage_choices':        Contact._meta.get_field('stage').choices,
    }
    response = render(request, 'crm/dashboard.html', context)
    response['Cache-Control'] = 'no-store'
    return response


# ── Stage expanded list ────────────────────────────────────────────────────────

@workspace_required
def stage_list(request, model_type, stage, workspace, membership):
    valid_stages = {key for key, _, _ in STAGE_META}
    if stage not in valid_stages or model_type not in ('contacts', 'companies'):
        raise Http404

    stage_label, badge_class = next(
        (label, badge) for key, label, badge in STAGE_META if key == stage
    )

    if model_type == 'contacts':
        records     = Contact.objects.filter(workspace=workspace, stage=stage).order_by('-created_at')
        model_label = 'Contacts'
    else:
        records     = Company.objects.filter(workspace=workspace, stage=stage).order_by('-created_at')
        model_label = 'Companies'

    from .models import INDUSTRY_LIST
    return render(request, 'crm/stage_list.html', {
        'records':       records,
        'model_type':    model_type,
        'model_label':   model_label,
        'stage':         stage,
        'stage_label':   stage_label,
        'badge_class':   badge_class,
        'count':         records.count(),
        'workspace':     workspace,
        'industry_list': INDUSTRY_LIST,
        'source_choices': Contact._meta.get_field('source').choices,
        'stage_choices':  Contact._meta.get_field('stage').choices,
    })


# ── History API ────────────────────────────────────────────────────────────────

@_api_workspace_required
def get_history(request, model_type, pk, workspace, membership):
    settings = HeatSettings.get_for_workspace(workspace)
    if model_type == 'contact':
        obj = get_object_or_404(Contact, pk=pk, workspace=workspace)
        stage_label = obj.get_stage_display()
        stage_badge = next(badge for key, _, badge in STAGE_META if key == obj.stage)
        info = {
            'name':           obj.name,
            'model_type':     'contact',
            'subtitle':       ' · '.join(filter(None, [obj.role, obj.company])),
            'email':          obj.email,
            'linkedin':       obj.linkedin,
            'location':       obj.location,
            'industry':       obj.industry,
            'owner':          obj.relationship_owner,
            'stage_key':      obj.stage,
            'stage':          stage_label,
            'stage_badge':    stage_badge,
            'notes':          obj.notes,
            'heat':           obj.heat,
            'heat_override':  obj.heat_override,
            'heat_score':     calculate_score(obj, settings),
            'heat_auto':      auto_heat(obj, settings) if not obj.heat_override else None,
        }

    elif model_type == 'company':
        obj = get_object_or_404(Company, pk=pk, workspace=workspace)
        stage_label = obj.get_stage_display()
        stage_badge = next(badge for key, _, badge in STAGE_META if key == obj.stage)
        info = {
            'name':           obj.company_name,
            'model_type':     'company',
            'subtitle':       ' · '.join(filter(None, [obj.industry, obj.hq_location])),
            'email':          '',
            'linkedin':       '',
            'website':        obj.website,
            'size':           obj.size,
            'funding':        obj.funding_stage,
            'owner':          '',
            'stage_key':      obj.stage,
            'stage':          stage_label,
            'stage_badge':    stage_badge,
            'notes':          obj.notes,
            'heat':           obj.heat,
            'heat_override':  obj.heat_override,
            'heat_score':     calculate_score(obj, settings),
            'heat_auto':      auto_heat(obj, settings) if not obj.heat_override else None,
        }

    else:
        return JsonResponse({'error': 'Invalid model type'}, status=400)

    touchpoints = [
        {
            'id':         tp.id,
            'type':       tp.touchpoint_type,
            'type_label': tp.get_touchpoint_type_display(),
            'date':       str(tp.date),
            'summary':    tp.summary,
            'notes':      tp.notes,
            'logged_by':  tp.logged_by,
        }
        for tp in obj.touchpoints.all()
    ]

    email_threads = []
    if model_type == 'contact':
        email_threads = [
            {
                'direction': et.direction,
                'subject':   et.subject,
                'body':      et.body,
                'sent_at':   et.sent_at.isoformat(),
            }
            for et in obj.email_thread.order_by('sent_at')
        ]

    return JsonResponse({'info': info, 'touchpoints': touchpoints, 'email_threads': email_threads})


@_api_workspace_required
@require_http_methods(['PATCH'])
def update_record(request, model_type, pk, workspace, membership):
    if model_type == 'contact':
        obj = get_object_or_404(Contact, pk=pk, workspace=workspace)
    elif model_type == 'company':
        obj = get_object_or_404(Company, pk=pk, workspace=workspace)
    else:
        return JsonResponse({'error': 'Invalid model type'}, status=400)

    data = json.loads(request.body)

    if 'notes' in data:
        obj.notes = data['notes']

    if 'stage' in data:
        valid = {key for key, _, _ in STAGE_META}
        if data['stage'] in valid:
            obj.stage = data['stage']

    if 'heat' in data:
        from .models import HEAT_CHOICES
        valid_heats = {key for key, _ in HEAT_CHOICES}
        if data['heat'] in valid_heats:
            obj.heat = data['heat']
            obj.heat_override = True

    if data.get('heat_reset'):
        cfg = HeatSettings.get_for_workspace(workspace)
        obj.heat = auto_heat(obj, cfg)
        obj.heat_override = False

    obj.save()
    cfg = HeatSettings.get_for_workspace(workspace)
    return JsonResponse({
        'ok':          True,
        'stage_key':   obj.stage,
        'stage_label': obj.get_stage_display(),
        'heat':        obj.heat,
        'heat_score':  calculate_score(obj, cfg),
        'heat_override': obj.heat_override,
    })


def _maybe_send_outreach(contact, workspace, user, cfg=None, template_id=None):
    """Send cold outreach email for a contact if outreach is configured.
    Returns (True, 'sent') on success, (False, reason) if skipped or errored."""
    import uuid as _uuid, resend as _resend
    if cfg is None:
        cfg = HeatSettings.get_for_workspace(workspace)
    profile = UserProfile.get_for_user(user)
    if not profile.outreach_enabled:
        return False, 'outreach_disabled'
    if not cfg.resend_api_key:
        return False, 'no_resend_api_key'

    # Resolve template: explicit ID → workspace default → legacy profile fields
    tmpl_obj = None
    if template_id:
        tmpl_obj = EmailTemplate.objects.filter(pk=template_id, workspace=workspace).first()
    if tmpl_obj is None:
        tmpl_obj = EmailTemplate.objects.filter(workspace=workspace, is_default=True).first()

    if tmpl_obj:
        raw_subject = tmpl_obj.subject
        raw_body    = tmpl_obj.body
    else:
        if not profile.outreach_subject:
            return False, 'no_subject_template'
        if not profile.outreach_body:
            return False, 'no_body_template'
        raw_subject = profile.outreach_subject
        raw_body    = profile.outreach_body

    try:
        subject   = _render_template(raw_subject, contact, workspace)
        body      = _render_template(raw_body, contact, workspace)
        is_html   = '<' in body
        msg_id    = f'<{_uuid.uuid4()}@wvvy.pro>'
        from_addr = profile.from_email or cfg.resend_from_email or 'noreply@wvvy.pro'
        reply_to  = f'reply+{contact.pk}@{INBOUND_DOMAIN}'
        _resend.api_key = cfg.resend_api_key
        email_payload = {
            'from':     from_addr,
            'to':       [contact.email],
            'subject':  subject,
            'reply_to': reply_to,
            'headers':  {'Message-ID': msg_id},
            'tags': [
                {'name': 'model_type', 'value': 'contact'},
                {'name': 'object_id',  'value': str(contact.pk)},
            ],
        }
        if is_html:
            email_payload['html'] = body
        else:
            email_payload['text'] = body

        # Attach files: per-template attachments if using a named template,
        # otherwise workspace-level outreach attachments.
        import base64 as _b64
        if tmpl_obj:
            att_qs = EmailTemplateAttachment.objects.filter(email_template=tmpl_obj)
        else:
            att_qs = OutreachAttachment.objects.filter(workspace=workspace)
        attachments = [
            {'filename': a.filename,
             'content':  _b64.b64encode(bytes(a.file_data)).decode('ascii')}
            for a in att_qs
        ]
        if attachments:
            email_payload['attachments'] = attachments

        _resend.Emails.send(email_payload)
        EmailThread.objects.create(
            contact=contact, message_id=msg_id,
            direction='outbound', subject=subject, body=body,
        )
        contact.last_message_id = msg_id
        contact.save(update_fields=['last_message_id'])
        ct = ContentType.objects.get_for_model(Contact)
        TouchPoint.objects.create(
            content_type=ct, object_id=contact.pk,
            touchpoint_type='email', date=date.today(),
            summary=f'Auto outreach: {subject}',
            notes=body[:500], logged_by='WVVYphone (Auto)',
        )
        contact.heat = auto_heat(contact, cfg)
        contact.save(update_fields=['heat'])
        return True, 'sent'
    except Exception as _exc:
        import traceback
        traceback.print_exc()
        return False, f'error: {_exc}'


@_api_workspace_required
@require_POST
def create_record(request, model_type, workspace, membership):
    data = json.loads(request.body)

    if model_type == 'contact':
        valid_stages  = {k for k, _ in Contact._meta.get_field('stage').choices}
        valid_sources = {k for k, _ in Contact._meta.get_field('source').choices}
        obj = Contact.objects.create(
            workspace          = workspace,
            name               = data.get('name', '').strip(),
            email              = data.get('email', '').strip(),
            phone              = data.get('phone', '').strip(),
            company            = data.get('company', '').strip(),
            role               = data.get('role', '').strip(),
            linkedin           = data.get('linkedin', '').strip(),
            location           = data.get('location', '').strip(),
            industry           = data.get('industry', '').strip(),
            source             = data.get('source', '') if data.get('source') in valid_sources else '',
            relationship_owner = data.get('relationship_owner', '').strip(),
            notes              = data.get('notes', '').strip(),
            stage              = data.get('stage', 'cold_lead') if data.get('stage') in valid_stages else 'cold_lead',
        )
        cfg = HeatSettings.get_for_workspace(workspace)
        obj.heat = auto_heat(obj, cfg)
        obj.save(update_fields=['heat'])

        # Send cold outreach email if this is a cold lead with an email address
        email_status = None
        if obj.stage == 'cold_lead' and obj.email:
            template_id = data.get('template_id') or None
            _, email_status = _maybe_send_outreach(obj, workspace, request.user, cfg,
                                                   template_id=template_id)

        return JsonResponse({'ok': True, 'id': obj.pk, 'name': obj.name, 'email_status': email_status})

    elif model_type == 'company':
        obj = Company.objects.create(
            workspace        = workspace,
            company_name     = data.get('company_name', '').strip(),
            website          = data.get('website', '').strip(),
            industry         = data.get('industry', '').strip(),
            size             = data.get('size', '').strip(),
            funding_stage    = data.get('funding_stage', '').strip(),
            product_category = data.get('product_category', '').strip(),
            hq_location      = data.get('hq_location', '').strip(),
            notes            = data.get('notes', '').strip(),
            stage            = data.get('stage', 'cold_lead'),
        )
        return JsonResponse({'ok': True, 'id': obj.pk, 'name': obj.company_name})

    elif model_type == 'opportunity':
        obj = Opportunity.objects.create(
            workspace         = workspace,
            company           = data.get('company', '').strip(),
            contact           = data.get('contact', '').strip(),
            estimated_value   = data.get('estimated_value') or 0,
            service_needed    = data.get('service_needed', ''),
            stage             = data.get('stage', 'prospect'),
            probability       = data.get('probability') or 0,
            expected_timeline = data.get('expected_timeline', '').strip(),
            notes             = data.get('notes', '').strip(),
        )
        return JsonResponse({'ok': True, 'id': obj.pk, 'name': obj.company})

    return JsonResponse({'error': 'Invalid model type'}, status=400)


_STAGE_ORDER = ['cold_lead', 'warm_lead', 'discovery_call', 'proposal', 'negotiation', 'closed_won']
_STAGE_LABEL = {k: l for k, l, _ in STAGE_META}


def _advance_stage(current_stage, tp_type, outcome):
    """Return the new stage key, or None if it should not change."""
    def rank(s):
        try:
            return _STAGE_ORDER.index(s)
        except ValueError:
            return -1

    def advance_to(target):
        return target if rank(target) > rank(current_stage) else None

    if current_stage == 'closed_won':
        return None

    if outcome == 'not_interested':
        return 'closed_lost'

    if outcome == 'booked':
        return advance_to('discovery_call')

    if tp_type == 'meeting':
        if current_stage in ('cold_lead', 'warm_lead'):
            return 'discovery_call'
        if current_stage == 'discovery_call':
            return advance_to('proposal')
        if current_stage == 'proposal':
            return advance_to('negotiation')

    if tp_type in ('call', 'voicemail', 'text'):
        if outcome == 'interested':
            if current_stage == 'cold_lead':
                return 'warm_lead'
            return advance_to('discovery_call')
        return advance_to('warm_lead')

    return None


@_api_workspace_required
@require_POST
def add_touchpoint(request, model_type, pk, workspace, membership):
    if model_type == 'contact':
        obj = get_object_or_404(Contact, pk=pk, workspace=workspace)
        ct  = ContentType.objects.get_for_model(Contact)
    elif model_type == 'company':
        obj = get_object_or_404(Company, pk=pk, workspace=workspace)
        ct  = ContentType.objects.get_for_model(Company)
    else:
        return JsonResponse({'error': 'Invalid model type'}, status=400)

    data = json.loads(request.body)
    tp = TouchPoint.objects.create(
        content_type    = ct,
        object_id       = pk,
        touchpoint_type = data.get('type', 'other'),
        date            = data.get('date'),
        summary         = data.get('summary', ''),
        notes           = data.get('notes', ''),
        outcome         = data.get('outcome', ''),
        logged_by       = data.get('logged_by', ''),
    )

    update_fields = []
    if not obj.heat_override:
        cfg = HeatSettings.get_for_workspace(workspace)
        obj.heat = auto_heat(obj, cfg)
        update_fields.append('heat')

    called_updated = False
    stage_changed  = False
    new_stage = obj.stage if hasattr(obj, 'stage') else None
    if model_type == 'contact':
        if tp.touchpoint_type == 'call' and not obj.called:
            obj.called = True
            called_updated = True
            update_fields.append('called')
        next_stage = _advance_stage(obj.stage, tp.touchpoint_type, tp.outcome)
        if next_stage:
            obj.stage  = next_stage
            new_stage  = next_stage
            stage_changed = True
            update_fields.append('stage')

    if update_fields:
        obj.save(update_fields=update_fields)

    return JsonResponse({
        'id':            tp.id,
        'type':          tp.touchpoint_type,
        'type_label':    tp.get_touchpoint_type_display(),
        'date':          str(tp.date),
        'summary':       tp.summary,
        'notes':         tp.notes,
        'outcome':       tp.outcome,
        'logged_by':     tp.logged_by,
        'called_updated': called_updated,
        'stage_changed': stage_changed,
        'new_stage':     new_stage,
        'new_stage_label': _STAGE_LABEL.get(new_stage, '') if stage_changed else '',
    })


@workspace_required
@ensure_csrf_cookie
def settings_view(request, workspace, membership):
    from .models import INDUSTRY_LIST
    settings = HeatSettings.get_for_workspace(workspace)
    profile  = UserProfile.get_for_user(request.user)
    members  = WorkspaceMembership.objects.filter(workspace=workspace).select_related('user').order_by('joined_at')
    signals = [
        {'field': 'pts_ideal_industry', 'label': 'Ideal Industry',        'description': 'Contact or company is in one of the selected ideal industries', 'value': settings.pts_ideal_industry},
        {'field': 'pts_raised_funding',  'label': 'Raised Funding',        'description': 'Company has raised funding (non-bootstrapped)',                   'value': settings.pts_raised_funding},
        {'field': 'pts_product_launch',  'label': 'Large Product Launch',  'description': 'A Product Launch touchpoint has been logged',                     'value': settings.pts_product_launch},
        {'field': 'pts_referral',        'label': 'Referral',              'description': 'Contact was sourced via a referral',                              'value': settings.pts_referral},
        {'field': 'pts_email_opened',    'label': 'Email Interaction',     'description': 'An email touchpoint has been logged',                             'value': settings.pts_email_opened},
        {'field': 'pts_responded',       'label': 'Responded to Outreach', 'description': 'A LinkedIn interaction touchpoint has been logged',               'value': settings.pts_responded},
        {'field': 'pts_meeting_booked',  'label': 'Meeting Booked',        'description': 'A meeting touchpoint has been logged',                            'value': settings.pts_meeting_booked},
    ]
    outreach_vars = [
        {'key': '{{name}}',         'label': 'Full name'},
        {'key': '{{first_name}}',   'label': 'First name'},
        {'key': '{{company}}',      'label': 'Company'},
        {'key': '{{title}}',        'label': 'Job title'},
        {'key': '{{industry}}',     'label': 'Industry'},
        {'key': '{{location}}',     'label': 'Location'},
        {'key': '{{meeting_link}}', 'label': 'Booking URL'},
        {'key': '{{signature}}',    'label': 'Signature'},
    ]
    templates  = list(workspace.email_templates.values('id', 'name', 'subject', 'body', 'is_default'))
    img_list   = [{'id': i.pk, 'name': i.name, 'url': i.image.url}
                  for i in workspace.email_images.order_by('-uploaded_at')]
    outreach_atts = list(workspace.outreach_attachments.values('id', 'filename', 'file_size'))
    from django.conf import settings as django_conf
    db_engine = django_conf.DATABASES['default']['ENGINE']
    db_label  = 'PostgreSQL' if 'postgresql' in db_engine or 'postgis' in db_engine else 'SQLite (WARNING: ephemeral!)'

    return render(request, 'crm/settings.html', {
        'settings':           settings,
        'profile':            profile,
        'industry_list':      INDUSTRY_LIST,
        'ideal':              settings.get_ideal_industries(),
        'signals':            signals,
        'outreach_vars':      outreach_vars,
        'workspace':          workspace,
        'membership':         membership,
        'members':            members,
        'is_admin':           _is_admin(request, membership),
        'is_owner':           (membership and membership.role == 'owner') or request.user.email == MASTER_EMAIL,
        'db_label':           db_label,
        'settings_pk':        settings.pk,
        'email_templates':      templates,
        'email_templates_json': json.dumps(templates),
        'email_images':         img_list,
        'outreach_attachments': outreach_atts,
        'outreach_attachments_json': json.dumps(outreach_atts),
        'active_backup_job': (
            TaskJob.objects
            .filter(workspace=workspace, task_type='backup_outreach',
                    status__in=['pending', 'running'])
            .order_by('-created_at')
            .values('id', 'status', 'phase', 'emails_total', 'emails_sent', 'emails_skipped')
            .first()
        ),
        'nav_items':          [
            {'id': 'leads',     'label': 'Leads'},
            {'id': 'email',     'label': 'Email'},
            {'id': 'outreach',  'label': 'Outreach'},
            {'id': 'workspace', 'label': 'Workspace'},
            {'id': 'team',      'label': 'Team'},
        ] + ([{'id': 'wvvy', 'label': 'WVVY Only'}] if request.user.email == MASTER_EMAIL else []),
    })


@_api_workspace_required
@require_POST
def save_settings(request, workspace, membership):
    import json as _json
    data     = _json.loads(request.body)
    settings = HeatSettings.get_for_workspace(workspace)

    # Per-user profile fields
    profile = UserProfile.get_for_user(request.user)
    for field in ('from_email', 'outreach_subject', 'outreach_body'):
        if field in data:
            setattr(profile, field, data[field])
    if 'outreach_enabled' in data:
        profile.outreach_enabled = bool(data['outreach_enabled'])
    profile.save()

    int_fields = [
        'pts_ideal_industry', 'pts_raised_funding', 'pts_product_launch',
        'pts_referral', 'pts_email_opened', 'pts_responded', 'pts_meeting_booked',
        'thresh_cold', 'thresh_medium', 'thresh_warm',
        'drip_interval_days', 'drip_max_followups',
    ]
    for field in int_fields:
        if field in data:
            setattr(settings, field, int(data[field]))

    if 'ideal_industries' in data:
        settings.ideal_industries = _json.dumps(data['ideal_industries'])

    for field in ('resend_api_key', 'resend_from_email', 'resend_webhook_secret',
                  'reply_to_domain', 'signature',
                  'outreach_subject', 'outreach_body'):
        if field in data:
            setattr(settings, field, data[field])

    if 'calendar_booking_url' in data:
        settings.calendar_booking_url = data['calendar_booking_url'].strip()

    if 'outreach_enabled' in data:
        settings.outreach_enabled = bool(data['outreach_enabled'])

    if 'ai_review_mode' in data:
        settings.ai_review_mode = bool(data['ai_review_mode'])

    settings.save()

    # Recalculate heat for workspace records only
    for obj in Contact.objects.filter(workspace=workspace, heat_override=False):
        obj.heat = auto_heat(obj, settings)
        obj.save(update_fields=['heat'])
    for obj in Company.objects.filter(workspace=workspace, heat_override=False):
        obj.heat = auto_heat(obj, settings)
        obj.save(update_fields=['heat'])

    return JsonResponse({'ok': True})


# ── Email Templates ────────────────────────────────────────────────────────────

@_api_workspace_required
def email_templates_list(request, workspace, membership):
    templates = list(workspace.email_templates.values(
        'id', 'name', 'subject', 'body', 'is_default', 'created_at'))
    # Attach per-template attachment lists (exclude file_data — UI only needs metadata)
    tmpl_ids = [t['id'] for t in templates]
    att_qs   = EmailTemplateAttachment.objects.filter(
        email_template_id__in=tmpl_ids).values('id', 'email_template_id', 'filename', 'file_size')
    att_map  = {}
    for a in att_qs:
        att_map.setdefault(a['email_template_id'], []).append(
            {'id': a['id'], 'filename': a['filename'], 'file_size': a['file_size']})
    for t in templates:
        t['attachments'] = att_map.get(t['id'], [])
    return JsonResponse({'templates': templates})


@_api_workspace_required
@require_POST
def email_template_save(request, workspace, membership):
    data = json.loads(request.body)
    pk   = data.get('id')
    if pk:
        tmpl = get_object_or_404(EmailTemplate, pk=pk, workspace=workspace)
    else:
        tmpl = EmailTemplate(workspace=workspace)
    tmpl.name    = data.get('name', '').strip() or 'Untitled'
    tmpl.subject = data.get('subject', '').strip()
    tmpl.body    = data.get('body', '').strip()
    if data.get('is_default'):
        workspace.email_templates.exclude(pk=tmpl.pk).update(is_default=False)
        tmpl.is_default = True
    tmpl.save()
    return JsonResponse({'ok': True, 'id': tmpl.pk, 'name': tmpl.name})


@_api_workspace_required
@require_POST
def email_template_delete(request, pk, workspace, membership):
    tmpl = get_object_or_404(EmailTemplate, pk=pk, workspace=workspace)
    tmpl.delete()
    return JsonResponse({'ok': True})


@_api_workspace_required
@require_POST
def email_template_set_default(request, pk, workspace, membership):
    workspace.email_templates.update(is_default=False)
    tmpl = get_object_or_404(EmailTemplate, pk=pk, workspace=workspace)
    tmpl.is_default = True
    tmpl.save(update_fields=['is_default'])
    return JsonResponse({'ok': True})


# ── Email Attachments ─────────────────────────────────────────────────────────

# Resend's max total email size is 40 MB — enforce per-file to keep things simple.
_ATTACHMENT_MAX_BYTES = 40 * 1024 * 1024


@_api_workspace_required
@require_POST
def upload_outreach_attachment(request, workspace, membership):
    """Upload a file to the workspace-level outreach attachment list."""
    f = request.FILES.get('file')
    if not f:
        return JsonResponse({'error': 'No file provided'}, status=400)
    if f.size > _ATTACHMENT_MAX_BYTES:
        return JsonResponse({'error': 'File exceeds the 40 MB limit'}, status=400)
    att = OutreachAttachment.objects.create(
        workspace    = workspace,
        filename     = f.name,
        content_type = f.content_type or 'application/octet-stream',
        file_size    = f.size,
        file_data    = f.read(),
    )
    return JsonResponse({'ok': True, 'id': att.pk, 'filename': att.filename,
                         'file_size': att.file_size, 'content_type': att.content_type})


@_api_workspace_required
@require_POST
def delete_outreach_attachment(request, pk, workspace, membership):
    att = get_object_or_404(OutreachAttachment, pk=pk, workspace=workspace)
    att.delete()
    return JsonResponse({'ok': True})


@_api_workspace_required
@require_POST
def upload_email_template_attachment(request, pk, workspace, membership):
    """Upload a file attached to a specific named EmailTemplate (pk = template pk)."""
    tmpl = get_object_or_404(EmailTemplate, pk=pk, workspace=workspace)
    f = request.FILES.get('file')
    if not f:
        return JsonResponse({'error': 'No file provided'}, status=400)
    if f.size > _ATTACHMENT_MAX_BYTES:
        return JsonResponse({'error': 'File exceeds the 40 MB limit'}, status=400)
    att = EmailTemplateAttachment.objects.create(
        email_template = tmpl,
        filename       = f.name,
        content_type   = f.content_type or 'application/octet-stream',
        file_size      = f.size,
        file_data      = f.read(),
    )
    return JsonResponse({'ok': True, 'id': att.pk, 'filename': att.filename,
                         'file_size': att.file_size, 'content_type': att.content_type})


@_api_workspace_required
@require_POST
def delete_email_template_attachment(request, pk, workspace, membership):
    """Delete an EmailTemplateAttachment by its pk."""
    att = get_object_or_404(EmailTemplateAttachment, pk=pk)
    if att.email_template.workspace_id != workspace.pk:
        return JsonResponse({'error': 'Not found'}, status=404)
    att.delete()
    return JsonResponse({'ok': True})


# ── Email Images ───────────────────────────────────────────────────────────────

@_api_workspace_required
def email_images_list(request, workspace, membership):
    images = [{'id': i.pk, 'name': i.name, 'url': i.image.url}
              for i in workspace.email_images.order_by('-uploaded_at')]
    return JsonResponse({'images': images})


@_api_workspace_required
@require_POST
def upload_email_image(request, workspace, membership):
    img_file = request.FILES.get('image')
    if not img_file:
        return JsonResponse({'error': 'No file provided'}, status=400)
    try:
        img = EmailImage.objects.create(
            workspace=workspace,
            name=img_file.name,
            image=img_file,
        )
        return JsonResponse({'ok': True, 'id': img.pk, 'name': img.name, 'url': img.image.url})
    except Exception:
        logger.exception('upload_email_image failed')
        return JsonResponse({'error': 'Failed to upload image'}, status=500)


@_api_workspace_required
@require_POST
def delete_email_image(request, pk, workspace, membership):
    img = get_object_or_404(EmailImage, pk=pk, workspace=workspace)
    img.image.delete(save=False)
    img.delete()
    return JsonResponse({'ok': True})


# ── Send email via Resend ──────────────────────────────────────────────────────

@_api_workspace_required
@require_POST
def send_email(request, model_type, pk, workspace, membership):
    if model_type != 'contact':
        return JsonResponse({'error': 'Emails can only be sent to contacts'}, status=400)

    obj = get_object_or_404(Contact, pk=pk, workspace=workspace)
    if not obj.email:
        return JsonResponse({'error': 'This contact has no email address'}, status=400)

    cfg = HeatSettings.get_for_workspace(workspace)
    if not cfg.resend_api_key:
        return JsonResponse({'error': 'Resend API key not configured — add it in Settings'}, status=400)

    data    = json.loads(request.body)
    subject = data.get('subject', '').strip()
    body    = data.get('body', '').strip()
    if not subject or not body:
        return JsonResponse({'error': 'Subject and message are required'}, status=400)

    import resend
    resend.api_key = cfg.resend_api_key
    profile   = UserProfile.get_for_user(request.user)
    from_addr = profile.from_email or cfg.resend_from_email or 'WVVYphone <noreply@wvvy.pro>'

    import uuid as _uuid
    from .models import EmailThread
    msg_id = f'<{_uuid.uuid4()}@wvvy.pro>'

    msg = {
        'from':    from_addr,
        'to':      [obj.email],
        'subject': subject,
        'text':    body,
        'headers': {'Message-ID': msg_id},
        'tags':    [
            {'name': 'model_type', 'value': 'contact'},
            {'name': 'object_id',  'value': str(pk)},
        ],
    }
    reply_domain = cfg.reply_to_domain or INBOUND_DOMAIN
    msg['reply_to'] = f'reply+{pk}@{reply_domain}'

    try:
        resend.Emails.send(msg)
    except Exception:
        logger.exception('send_email failed')
        return JsonResponse({'error': 'Failed to send email'}, status=500)

    EmailThread.objects.create(
        contact=obj, message_id=msg_id,
        direction='outbound', subject=subject, body=body,
    )
    obj.last_message_id = msg_id
    obj.save(update_fields=['last_message_id'])

    ct = ContentType.objects.get_for_model(Contact)
    tp = TouchPoint.objects.create(
        content_type    = ct,
        object_id       = pk,
        touchpoint_type = 'email',
        date            = date.today(),
        summary         = f'Email sent: {subject}',
        notes           = body[:500],
        logged_by       = 'WVVYphone (Resend)',
    )
    if not obj.heat_override:
        obj.heat = auto_heat(obj, cfg)
        obj.save(update_fields=['heat'])

    return JsonResponse({
        'ok': True,
        'touchpoint': {
            'id':         tp.id,
            'type':       tp.touchpoint_type,
            'type_label': tp.get_touchpoint_type_display(),
            'date':       str(tp.date),
            'summary':    tp.summary,
            'notes':      tp.notes,
            'logged_by':  tp.logged_by,
        },
    })


# ── Resend webhook (email open tracking) ──────────────────────────────────────

@csrf_exempt
@require_POST
def resend_webhook(request):
    payload    = json.loads(request.body)
    event_type = payload.get('type')

    if event_type == 'email.opened':
        data     = payload.get('data', {})
        tags     = {t['name']: t['value'] for t in data.get('tags', [])}
        obj_type = tags.get('model_type')
        obj_id   = tags.get('object_id')

        if obj_type == 'contact' and obj_id:
            try:
                obj    = Contact.objects.get(pk=int(obj_id))
                ws_cfg = HeatSettings.get_for_workspace(obj.workspace)

                # Verify signature using the correct workspace's secret
                if ws_cfg.resend_webhook_secret:
                    try:
                        from svix.webhooks import Webhook
                        wh = Webhook(ws_cfg.resend_webhook_secret)
                        wh.verify(request.body, dict(request.headers))
                    except Exception:
                        return JsonResponse({'error': 'Invalid signature'}, status=400)

                ct = ContentType.objects.get_for_model(Contact)
                TouchPoint.objects.create(
                    content_type    = ct,
                    object_id       = obj.pk,
                    touchpoint_type = 'email',
                    date            = date.today(),
                    summary         = 'Email opened',
                    notes           = f'Subject: {data.get("subject", "")}',
                    logged_by       = 'Resend (auto)',
                )
                if not obj.heat_override:
                    obj.heat = auto_heat(obj, ws_cfg)
                    obj.save(update_fields=['heat'])
            except Contact.DoesNotExist:
                pass

    return JsonResponse({'ok': True})


# ── Manual "Mark Replied" ─────────────────────────────────────────────────────

@_api_workspace_required
@require_POST
def mark_replied(request, pk, workspace, membership):
    """One-click: log a 'Reply received' touchpoint for a contact."""
    obj = get_object_or_404(Contact, pk=pk, workspace=workspace)
    cfg  = HeatSettings.get_for_workspace(workspace)
    data = json.loads(request.body) if request.body else {}
    subject = data.get('subject', '').strip()

    ct = ContentType.objects.get_for_model(Contact)
    tp = TouchPoint.objects.create(
        content_type    = ct,
        object_id       = pk,
        touchpoint_type = 'email',
        date            = date.today(),
        summary         = f'Reply received{f": {subject}" if subject else ""}',
        notes           = data.get('notes', '').strip(),
        logged_by       = data.get('logged_by', 'Manual').strip() or 'Manual',
    )
    if not obj.heat_override:
        obj.heat = auto_heat(obj, cfg)
        obj.save(update_fields=['heat'])

    return JsonResponse({
        'ok': True,
        'touchpoint': {
            'id':         tp.id,
            'type':       tp.touchpoint_type,
            'type_label': tp.get_touchpoint_type_display(),
            'date':       str(tp.date),
            'summary':    tp.summary,
            'notes':      tp.notes,
            'logged_by':  tp.logged_by,
        },
    })


# ── Inbound email webhook + AI reply ──────────────────────────────────────────

@csrf_exempt
@require_POST
def inbound_webhook(request):
    """Receives inbound emails from Resend, matches to a contact, triggers AI reply."""
    try:
        return _handle_inbound(request)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('inbound_webhook unhandled error')
        return JsonResponse({'ok': True})


def _handle_inbound(request):
    import re
    from .models import EmailThread

    try:
        payload = json.loads(request.body)
        if isinstance(payload, list):
            payload = payload[0]
    except Exception:
        payload = request.POST.dict()

    # Resend wraps inbound in a type/data envelope
    if payload.get('type') == 'email.received':
        payload = payload.get('data', payload)

    to_raw   = payload.get('to') or payload.get('To') or payload.get('recipient') or ''
    if isinstance(to_raw, list):
        to_raw = ' '.join(to_raw)

    from_raw = payload.get('from') or payload.get('From') or payload.get('sender') or ''
    subject  = payload.get('subject') or payload.get('Subject') or ''
    email_id = payload.get('email_id') or payload.get('id') or ''

    # Resend inbound webhook exposes message_id as a top-level field (not in headers).
    inbound_message_id = payload.get('message_id', '')

    # Resend's inbound webhook does NOT include body text — only metadata.
    # Body must be fetched later via the Resend API once we have the contact's API key.
    body = (payload.get('text') or payload.get('Text') or
            payload.get('plain_body') or payload.get('body') or
            payload.get('html') or payload.get('Html') or '')

    # Extract plain sender email
    m = re.search(r'[\w.+%-]+@[\w.-]+\.[a-z]{2,}', from_raw, re.I)
    sender_email = m.group(0).lower() if m else ''

    # Match contact — first by reply+pk tag (preferred: unambiguous), then by sender email.
    # Contact PKs are globally unique so the reply+pk lookup is workspace-safe by design.
    contact = None
    pk_match = re.search(r'reply\+(\d+)@', to_raw)
    if pk_match:
        try:
            contact = Contact.objects.get(pk=int(pk_match.group(1)))
        except Contact.DoesNotExist:
            pass

    if contact is None and sender_email:
        # If the same email exists in multiple workspaces, prefer the one that was
        # actually emailed (has an outbound thread), falling back to most-recently-created.
        qs = Contact.objects.filter(email__iexact=sender_email)
        contact = (
            qs.filter(email_threads__direction='outbound')
              .order_by('-email_threads__created_at')
              .first()
        ) or qs.order_by('-created_at').first()

    if contact is None:
        return JsonResponse({'ok': True, 'skipped': 'no matching contact'})

    # Resend inbound webhook omits body by design — fetch via the receiving API.
    if not body and email_id:
        try:
            import requests as _requests
            cfg_ws = HeatSettings.get_for_workspace(contact.workspace)
            _resp = _requests.get(
                f'https://api.resend.com/emails/receiving/{email_id}',
                headers={'Authorization': f'Bearer {cfg_ws.resend_api_key}'},
                timeout=10,
            )
            email_obj = _resp.json()
            if isinstance(email_obj, dict):
                body = email_obj.get('text') or email_obj.get('html') or ''
        except Exception:
            pass

    if not body:
        body = f'[Lead replied to outreach email. Subject: "{subject}"]'

    # Save inbound thread entry
    EmailThread.objects.create(
        contact=contact, message_id=inbound_message_id, in_reply_to='',
        direction='inbound', subject=subject, body=body,
    )

    # Detect intent from the reply body
    intent = _detect_intent(body)

    # Capture this BEFORE we mutate the contact — used below to decide
    # whether to generate an AI draft regardless of what this email does
    # to sequence_stopped (interested replies set it True, but we still
    # want to draft a reply for the human to review/send).
    was_already_stopped = contact.sequence_stopped

    # Promote cold leads to warm when they reply; handle intent signals
    update_fields = []
    if contact.stage == 'cold_lead':
        contact.stage = 'warm_lead'
        update_fields.append('stage')

    if not contact.heat_override:
        contact.heat = auto_heat(contact, HeatSettings.get_for_workspace(contact.workspace))
        update_fields.append('heat')

    if intent == 'interested':
        # Flag for human follow-up, stop AI sequence
        if not contact.needs_attention:
            contact.needs_attention = True
            update_fields.append('needs_attention')
        if not contact.sequence_stopped:
            contact.sequence_stopped = True
            update_fields.append('sequence_stopped')
        if contact.stage not in ('warm_lead', 'discovery_call', 'proposal', 'negotiation', 'closed_won'):
            contact.stage = 'warm_lead'
            if 'stage' not in update_fields:
                update_fields.append('stage')
    elif intent == 'not_interested':
        if not contact.sequence_stopped:
            contact.sequence_stopped = True
            update_fields.append('sequence_stopped')

    # Any reply stops the drip sequence
    if not contact.drip_sequence_stopped:
        contact.drip_sequence_stopped = True
        update_fields.append('drip_sequence_stopped')

    if update_fields:
        contact.save(update_fields=update_fields)

    # Log touchpoint
    ct = ContentType.objects.get_for_model(Contact)
    TouchPoint.objects.create(
        content_type    = ct,
        object_id       = contact.pk,
        touchpoint_type = 'email',
        date            = date.today(),
        summary         = f'Reply received{f": {subject}" if subject else ""}',
        notes           = f'From: {from_raw}',
        logged_by       = 'Inbound (auto)',
    )

    # If not interested, delete the contact and stop
    if intent == 'not_interested':
        contact.delete()
        return JsonResponse({'ok': True, 'contact_id': 'deleted_not_interested'})

    # Generate AI reply/draft if the contact was reachable before this email.
    # Use was_already_stopped (not contact.sequence_stopped) so that "interested"
    # replies — which set sequence_stopped=True above — still get a draft queued
    # for human review rather than silently receiving no response.
    if contact.ai_managed and not was_already_stopped:
        _send_ai_reply(contact, subject, inbound_message_id)

    # Update drip training outcome scores — must run AFTER all existing logic
    # (intent detection, sequence_stopped mutation, AI reply generation).
    try:
        from .drip import update_outcome_scores_for_contact
        update_outcome_scores_for_contact(contact)
    except Exception:
        pass  # never break inbound flow due to training pipeline errors

    return JsonResponse({'ok': True, 'contact_id': contact.pk})


_COUNTRY_TZ = {
    'united states': 'America/New_York', 'usa': 'America/New_York',
    'canada': 'America/Toronto', 'united kingdom': 'Europe/London',
    'uk': 'Europe/London', 'ireland': 'Europe/Dublin',
    'australia': 'Australia/Sydney', 'new zealand': 'Pacific/Auckland',
    'germany': 'Europe/Berlin', 'france': 'Europe/Paris',
    'netherlands': 'Europe/Amsterdam', 'belgium': 'Europe/Brussels',
    'spain': 'Europe/Madrid', 'italy': 'Europe/Rome',
    'sweden': 'Europe/Stockholm', 'norway': 'Europe/Oslo',
    'denmark': 'Europe/Copenhagen', 'finland': 'Europe/Helsinki',
    'switzerland': 'Europe/Zurich', 'austria': 'Europe/Vienna',
    'poland': 'Europe/Warsaw', 'india': 'Asia/Kolkata',
    'singapore': 'Asia/Singapore', 'hong kong': 'Asia/Hong_Kong',
    'japan': 'Asia/Tokyo', 'china': 'Asia/Shanghai',
    'south korea': 'Asia/Seoul', 'brazil': 'America/Sao_Paulo',
    'mexico': 'America/Mexico_City', 'south africa': 'Africa/Johannesburg',
    'uae': 'Asia/Dubai', 'united arab emirates': 'Asia/Dubai',
    'israel': 'Asia/Jerusalem', 'nigeria': 'Africa/Lagos',
}

def _location_to_timezone(location_str):
    """Best-effort IANA timezone from a location string."""
    lower = (location_str or '').lower()
    for country, tz in _COUNTRY_TZ.items():
        if country in lower:
            return tz
    return 'UTC'


_NOT_INTERESTED_PHRASES = [
    'not interested', 'no thanks', 'no thank you', 'unsubscribe',
    'remove me', 'stop emailing', 'stop contacting', "don't contact",
    'do not contact', 'not a good fit', 'opt out', 'please stop',
    "don't reach out", 'wrong person', 'not the right person',
]
_INTERESTED_PHRASES = [
    'interested', 'tell me more', 'sounds good', "let's chat",
    "let's talk", 'book a call', 'schedule a call', 'set up a call',
    'set up a meeting', 'would love', 'happy to chat', 'can we meet',
    'yes please', 'when can we', 'what time works', "i'm in",
    'love to learn', 'sounds interesting', 'more information',
    'how does this work', 'let me know more', 'open to it',
]

def _detect_intent(body):
    """Returns 'interested', 'not_interested', or 'neutral'."""
    lower = (body or '').lower()
    for phrase in _NOT_INTERESTED_PHRASES:
        if phrase in lower:
            return 'not_interested'
    for phrase in _INTERESTED_PHRASES:
        if phrase in lower:
            return 'interested'
    return 'neutral'


def _send_ai_reply(contact, inbound_subject, inbound_message_id, inbound_in_reply_to=''):
    """Generate a Claude reply and send it via Resend. Never raises — logs failures instead."""
    import uuid as _uuid, os, anthropic, logging
    import resend as resend_sdk
    from .models import EmailThread, AICallLog

    log = logging.getLogger(__name__)

    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        AICallLog.objects.create(contact=contact, prompt='', response='SKIP: no ANTHROPIC_API_KEY', flagged=True)
        return

    workspace = contact.workspace
    cfg       = HeatSettings.get_for_workspace(workspace)
    if not cfg.resend_api_key:
        AICallLog.objects.create(contact=contact, prompt='', response='SKIP: no resend_api_key in HeatSettings', flagged=True)
        return

    # Build thread history as Claude messages
    thread = list(contact.email_thread.order_by('sent_at'))
    if not thread:
        AICallLog.objects.create(contact=contact, prompt='', response='SKIP: no email thread entries', flagged=True)
        return

    messages = []
    for entry in thread:
        if not entry.body or not entry.body.strip():
            continue  # skip empty bodies — Claude rejects blank content
        role = 'assistant' if entry.direction == 'outbound' else 'user'
        # Merge consecutive same-role messages (Claude requires alternating)
        if messages and messages[-1]['role'] == role:
            messages[-1]['content'] += f'\n\n{entry.body}'
        else:
            messages.append({'role': role, 'content': entry.body})

    # Claude requires messages to start with 'user' — drop any leading assistant turns
    while messages and messages[0]['role'] == 'assistant':
        messages.pop(0)

    # Must end on a user (inbound) message
    if not messages or messages[-1]['role'] != 'user':
        thread_debug = [(e.direction, len(e.body or '')) for e in thread]
        AICallLog.objects.create(contact=contact, prompt=str(messages), response=f'SKIP: messages empty or ends on assistant | thread={thread_debug}', flagged=True)
        return

    booking_url = cfg.calendar_booking_url or ''

    # Use workspace-level from address, fall back to owner profile
    owner_profile = UserProfile.get_for_user(workspace.owner)
    from_addr     = cfg.resend_from_email or owner_profile.from_email or 'noreply@wvvy.pro'

    # Parse sender's display name for the system prompt
    import re as _re
    name_match   = _re.match(r'^([^<]+)<', from_addr)
    sender_name  = name_match.group(1).strip() if name_match else from_addr.split('@')[0]
    sender_first = sender_name.split()[0] if sender_name else 'me'

    location_hint = contact.location or ''
    industry_hint = contact.industry or ''
    context_line  = ''
    if location_hint or industry_hint:
        context_line = (
            f"Their background: {', '.join(filter(None, [location_hint, industry_hint]))}. "
            f"If natural, weave in a brief, specific observation about their location or industry "
            f"(one short clause only, never formulaic).\n"
        )

    system_prompt = (
        f"You are a friendly email assistant replying to inbound emails on behalf of {sender_name}.\n\n"
        f"## Your One Job\n"
        f"Get the person to book a call. Every reply must end with the calendar link. "
        f"Do not answer questions, provide pricing, or go into detail about the business — that is what the call is for.\n\n"
        f"## Tone & Length\n"
        f"- Warm, friendly, and brief — 3 to 5 sentences maximum\n"
        f"- Never use salesy or pushy language\n"
        f"- Sound like a real person, not a bot\n"
        f"- Match the energy of the email — if they are casual, be casual; if formal, be professional\n\n"
        f"## What to Do\n"
        f"1. Acknowledge what they reached out about in one sentence\n"
        f"2. Express genuine interest in connecting\n"
        f"3. Direct them to book a time using the calendar link\n\n"
        f"## What NOT to Do\n"
        f"- Do not answer product questions, pricing questions, or anything requiring detail\n"
        f"- Do not write long emails\n"
        f"- Do not make promises or commitments on behalf of the business\n"
        f"- Do not use filler phrases like 'Great question!' or 'Absolutely!'\n"
        f"- Never use em dashes. Use a comma or period instead.\n"
        f"- Never use placeholder text, brackets, or templated phrases.\n\n"
        f"## Auto-Responder Detection\n"
        f"DO NOT REPLY to any of the following:\n"
        f"- Out of office or vacation replies (e.g. 'I am away until...', 'I am out of the office...')\n"
        f"- Delivery failure or bounce messages (e.g. 'Mail delivery failed', 'Undeliverable')\n"
        f"- 'No longer at this company' messages\n"
        f"- Role-change or forwarding notices\n"
        f"- Subscription confirmations or automated notifications\n"
        f"- Any reply where the sender is clearly a system, bot, or no-reply address\n"
        f"- Any email indicating it is an auto-reply (Auto-Submitted, X-Autoreply, Precedence: bulk)\n\n"
        f"If you detect any of the above, output exactly: DO_NOT_SEND — and nothing else.\n\n"
        f"## Context\n"
        f"Prospect: {contact.name}"
        f"{f', {contact.role}' if contact.role else ''}"
        f"{f' at {contact.company}' if contact.company else ''}.\n"
        f"{context_line}"
        f"{f'Calendar link: {booking_url}' if booking_url else 'No calendar link is configured — ask them to share their availability instead.'}\n\n"
        f"Sign off as '{sender_first}' only — no last name, no title."
    )

    try:
        client      = anthropic.Anthropic(api_key=api_key)
        ai_response = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1024,
            system=system_prompt,
            messages=messages,
        )
        reply_text    = ai_response.content[0].text.strip().replace('\u2014', ',').replace('\u2013', '-')
        input_tokens  = ai_response.usage.input_tokens
        output_tokens = ai_response.usage.output_tokens
    except Exception as e:
        AICallLog.objects.create(
            contact=contact, prompt=str(messages),
            response=f'API ERROR: {e}', flagged=True,
        )
        return

    # AI detected an auto-reply / bounce / OOO — suppress without sending
    if reply_text.strip() == 'DO_NOT_SEND':
        AICallLog.objects.create(
            contact=contact, prompt=str(messages),
            response='DO_NOT_SEND — auto-reply/bounce/OOO detected by AI, reply suppressed',
            flagged=True,
        )
        return

    # Safeguards — check for malformed / placeholder content
    BAD_PATTERNS = ['[your', '{{', '}}', '[booking', '[name', '[insert', '[contact', '[company']
    is_bad = (
        not reply_text
        or len(reply_text) < 10
        or any(p in reply_text.lower() for p in BAD_PATTERNS)
    )

    call_log = AICallLog.objects.create(
        contact=contact, prompt=str(messages), response=reply_text,
        input_tokens=input_tokens, output_tokens=output_tokens, flagged=is_bad,
    )

    if is_bad:
        log.warning('AI reply failed safeguard check for contact %s — flagged, not sent', contact.pk)
        return

    reply_subject      = inbound_subject if inbound_subject.lower().startswith('re:') else f'Re: {inbound_subject}'
    in_reply_to_header = inbound_message_id or contact.last_message_id

    # Review mode: save as pending and let a human approve/edit before sending
    if cfg.ai_review_mode:
        call_log.status               = 'pending'
        call_log.draft_subject        = reply_subject
        call_log.draft_inbound_msg_id = inbound_message_id
        call_log.draft_in_reply_to    = in_reply_to_header
        call_log.draft_is_followup    = False
        call_log.save(update_fields=['status', 'draft_subject', 'draft_inbound_msg_id',
                                     'draft_in_reply_to', 'draft_is_followup'])
        return

    _do_send_ai_email(
        contact, reply_text, reply_subject,
        inbound_message_id, in_reply_to_header, inbound_in_reply_to,
        cfg, from_addr, resend_sdk, call_log,
    )


def _do_send_ai_email(contact, reply_text, reply_subject, inbound_message_id,
                      in_reply_to_header, inbound_in_reply_to, cfg, from_addr, resend_sdk, call_log):
    """Send an AI-generated email via Resend and update all tracking state."""
    import uuid as _uuid
    from django.utils import timezone as _tz
    from .models import EmailThread

    new_message_id = f'<{_uuid.uuid4()}@wvvy.pro>'

    _ref_candidates = [inbound_in_reply_to, contact.last_message_id, inbound_message_id]
    _seen = set()
    _refs = []
    for r in _ref_candidates:
        if r and r not in _seen:
            _seen.add(r)
            _refs.append(r)
    references = ' '.join(_refs)

    reply_to_tag = f'reply+{contact.pk}@{INBOUND_DOMAIN}'
    resend_sdk.api_key = cfg.resend_api_key

    thread_headers = {'Message-ID': new_message_id}
    if in_reply_to_header:
        thread_headers['In-Reply-To'] = in_reply_to_header
    if references:
        thread_headers['References'] = references

    try:
        resend_sdk.Emails.send({
            'from':     from_addr,
            'to':       [contact.email],
            'subject':  reply_subject,
            'text':     reply_text,
            'reply_to': reply_to_tag,
            'headers':  thread_headers,
        })
    except Exception as e:
        call_log.response += f'\n\nSEND ERROR: {e}'
        call_log.flagged   = True
        call_log.save(update_fields=['response', 'flagged'])
        return

    EmailThread.objects.create(
        contact=contact, message_id=new_message_id, in_reply_to=inbound_message_id,
        direction='outbound', subject=reply_subject, body=reply_text,
    )
    contact.last_message_id   = new_message_id
    contact.follow_up_count   = (contact.follow_up_count or 0) + 1
    contact.last_follow_up_at = _tz.now()
    save_fields = ['last_message_id', 'follow_up_count', 'last_follow_up_at']
    if contact.follow_up_count >= 9:
        contact.sequence_stopped = True
        save_fields.append('sequence_stopped')
    contact.save(update_fields=save_fields)


def _send_ai_followup(contact):
    """Send a proactive follow-up email to a contact who hasn't replied. Never raises."""
    import uuid as _uuid, os, anthropic, logging
    import resend as resend_sdk
    from django.utils import timezone as _tz
    from .models import EmailThread, AICallLog

    log = logging.getLogger(__name__)

    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return

    workspace = contact.workspace
    cfg       = HeatSettings.get_for_workspace(workspace)
    if not cfg.resend_api_key:
        return

    thread = list(contact.email_thread.order_by('sent_at'))
    if not thread:
        return

    messages = []
    for entry in thread:
        if not entry.body or not entry.body.strip():
            continue
        role = 'assistant' if entry.direction == 'outbound' else 'user'
        if messages and messages[-1]['role'] == role:
            messages[-1]['content'] += f'\n\n{entry.body}'
        else:
            messages.append({'role': role, 'content': entry.body})

    while messages and messages[0]['role'] == 'assistant':
        messages.pop(0)

    if not messages:
        return

    # Add a synthetic trigger to prompt Claude to write a follow-up
    if messages[-1]['role'] == 'assistant':
        messages.append({'role': 'user', 'content': '[No reply yet.]'})

    booking_url  = cfg.calendar_booking_url or ''
    owner_profile = UserProfile.get_for_user(workspace.owner)
    from_addr     = cfg.resend_from_email or owner_profile.from_email or 'noreply@wvvy.pro'

    import re as _re
    name_match   = _re.match(r'^([^<]+)<', from_addr)
    sender_name  = name_match.group(1).strip() if name_match else from_addr.split('@')[0]
    sender_first = sender_name.split()[0] if sender_name else 'me'

    follow_up_num = (contact.follow_up_count or 0) + 1
    location_hint = contact.location or ''
    industry_hint = contact.industry or ''
    context_line  = ''
    if location_hint or industry_hint:
        context_line = (
            f"Their background: {', '.join(filter(None, [location_hint, industry_hint]))}. "
            f"If natural, weave in a brief specific observation (one short clause only, never formulaic).\n"
        )

    system_prompt = (
        f"You are a friendly email assistant writing a follow-up on behalf of {sender_name} "
        f"to a prospect who has not replied yet.\n\n"
        f"## Your One Job\n"
        f"Get the person to book a call. Every follow-up must end with the calendar link. "
        f"Do not answer questions or go into business detail — that is what the call is for.\n\n"
        f"## Tone & Length\n"
        f"- Warm, friendly, and brief — 3 to 5 sentences maximum\n"
        f"- Never use salesy or pushy language\n"
        f"- Sound like a real person checking in, not a bot\n"
        f"- Reference previous context naturally — do not repeat the same opener each time\n\n"
        f"## What NOT to Do\n"
        f"- Do not answer product questions or go into detail\n"
        f"- Do not make promises or commitments on behalf of the business\n"
        f"- Do not use filler phrases like 'Just checking in!' or 'Circling back!'\n"
        f"- Never use em dashes. Use a comma or period instead.\n"
        f"- Never use placeholder text, brackets, or templated phrases.\n\n"
        f"## Context\n"
        f"Prospect: {contact.name}"
        f"{f', {contact.role}' if contact.role else ''}"
        f"{f' at {contact.company}' if contact.company else ''}.\n"
        f"{context_line}"
        f"This is follow-up #{follow_up_num}.\n"
        f"{f'Calendar link: {booking_url}' if booking_url else 'No calendar link is configured — ask them to share their availability instead.'}\n\n"
        f"Sign off as '{sender_first}' only — no last name, no title."
    )

    try:
        client      = anthropic.Anthropic(api_key=api_key)
        ai_response = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=512,
            system=system_prompt,
            messages=messages,
        )
        reply_text    = ai_response.content[0].text.strip().replace('\u2014', ',').replace('\u2013', '-')
        input_tokens  = ai_response.usage.input_tokens
        output_tokens = ai_response.usage.output_tokens
    except Exception as e:
        AICallLog.objects.create(contact=contact, prompt=str(messages), response=f'FOLLOWUP API ERROR: {e}', flagged=True)
        return

    BAD_PATTERNS = ['[your', '{{', '}}', '[booking', '[name', '[insert', '[contact', '[company', '[no reply']
    is_bad = not reply_text or len(reply_text) < 10 or any(p in reply_text.lower() for p in BAD_PATTERNS)

    call_log = AICallLog.objects.create(
        contact=contact, prompt=str(messages), response=reply_text,
        input_tokens=input_tokens, output_tokens=output_tokens, flagged=is_bad,
    )

    if is_bad:
        log.warning('AI follow-up failed safeguard check for contact %s', contact.pk)
        return

    last_outbound  = contact.email_thread.filter(direction='outbound').order_by('-sent_at').first()
    last_subject   = last_outbound.subject if last_outbound else 'Following up'
    reply_subject  = last_subject if last_subject.lower().startswith('re:') else f'Re: {last_subject}'
    in_reply_to    = contact.last_message_id or ''

    if cfg.ai_review_mode:
        call_log.status               = 'pending'
        call_log.draft_subject        = reply_subject
        call_log.draft_inbound_msg_id = ''
        call_log.draft_in_reply_to    = in_reply_to
        call_log.draft_is_followup    = True
        call_log.save(update_fields=['status', 'draft_subject', 'draft_inbound_msg_id',
                                     'draft_in_reply_to', 'draft_is_followup'])
        return

    _do_send_ai_email(
        contact, reply_text, reply_subject,
        '',          # no inbound_message_id for proactive follow-ups
        in_reply_to, in_reply_to,
        cfg, from_addr, resend_sdk, call_log,
    )


# ── Email template renderer ───────────────────────────────────────────────────

def _render_template(template, contact, workspace=None):
    """Replace {{variable}} placeholders with contact field values."""
    first_name   = (contact.name or '').split()[0] if contact.name else ''
    cfg          = HeatSettings.get_for_workspace(workspace) if workspace else HeatSettings()
    meeting_link = cfg.calendar_booking_url or ''
    signature    = cfg.signature or ''
    for key, val in {
        '{{name}}':         contact.name or '',
        '{{first_name}}':   first_name,
        '{{company}}':      contact.company or '',
        '{{title}}':        contact.role or '',
        '{{industry}}':     contact.industry or '',
        '{{location}}':     contact.location or '',
        '{{meeting_link}}': meeting_link,
        '{{signature}}':    signature,
    }.items():
        template = template.replace(key, val)
    return template


# ── Advanced Search (Apify) ───────────────────────────────────────────────────

from datetime import datetime as _dt, timezone as _dt_tz

_SENIORITY_OPTIONS = [
    ('Founder', 'Founder'), ('Chairman', 'Chairman'), ('President', 'President'),
    ('CEO', 'CEO'), ('CXO', 'CXO / C-Suite'), ('Executive', 'Executive'),
    ('Vice President', 'Vice President'), ('Director', 'Director'), ('Head', 'Head'),
    ('Manager', 'Manager'), ('Senior', 'Senior'), ('Junior', 'Junior'),
    ('Entry Level', 'Entry Level'),
]

_FUNCTIONAL_OPTIONS = [
    ('Admin', 'Admin'), ('Analytics', 'Analytics'), ('Applications', 'Applications'),
    ('Cloud', 'Cloud'), ('Compliance', 'Compliance'), ('Controller', 'Controller'),
    ('Customer Service', 'Customer Service'), ('Cyber Security', 'Cyber Security'),
    ('Data Engineering', 'Data Engineering'), ('Devops', 'Devops'), ('Digital', 'Digital'),
    ('Distribution', 'Distribution'), ('Engineering', 'Engineering'), ('Finance', 'Finance'),
    ('Fraud', 'Fraud'), ('Hiring', 'Hiring'), ('HR', 'HR'),
    ('Infrastructure', 'Infrastructure'), ('Inside Sales', 'Inside Sales'), ('IT', 'IT'),
    ('Learning', 'Learning'), ('Legal', 'Legal'), ('Marketing', 'Marketing'),
    ('Network Security', 'Network Security'), ('Operations', 'Operations'),
    ('Product Management', 'Product Management'), ('Product Security', 'Product Security'),
    ('Production', 'Production'), ('Purchase', 'Purchase'), ('Research', 'Research'),
    ('Risk', 'Risk'), ('Sales', 'Sales'), ('Security', 'Security'),
    ('Support', 'Support'), ('Testing', 'Testing'), ('Training', 'Training'),
]

_COMPANY_SIZE_OPTIONS = [
    '0 - 1', '2 - 10', '11 - 50', '51 - 200', '201 - 500', '501 - 1000', '1001 - 5000', '5001 - 10000', '10000+',
]

_REVENUE_OPTIONS = [
    '< 1M', '1M-10M', '11M-100M', '101M-500M', '501M-1B', '1B+',
]

_BUSINESS_MODEL_OPTIONS = ['Product', 'Services', 'Solutions']

_EMAIL_STATUS_OPTIONS = [
    ('verified',   'Verified'),
    ('unverified', 'Unverified'),
    ('all',        'All'),
]


def _parse_apify_filters(post):
    """Build Apify actor input dict from POST data."""
    filters = {}

    def csv_list(key):
        val = post.get(key, '').strip()
        return [v.strip() for v in val.split(',') if v.strip()] if val else []

    if post.get('firstName'):
        filters['firstName'] = post['firstName'].strip()
    if post.get('lastName'):
        filters['lastName'] = post['lastName'].strip()

    titles = csv_list('personTitle')
    if titles:
        filters['personTitle'] = titles

    seniority = post.getlist('seniority')
    if seniority:
        filters['seniority'] = seniority

    functional = post.getlist('functional')
    if functional:
        filters['functional'] = functional

    person_country = csv_list('personCountry')
    if person_country:
        filters['personCountry'] = person_country


    email_status = post.get('contactEmailStatus', 'all')
    if email_status and email_status != 'all':
        filters['contactEmailStatus'] = email_status

    company_domains = csv_list('companyDomain')
    if company_domains:
        filters['companyDomain'] = company_domains

    company_country = csv_list('companyCountry')
    if company_country:
        filters['companyCountry'] = company_country


    # industry is routed to industryKeywords (free-text) because the strict
    # `industry` enum has ~479 exact LinkedIn values that users can't reliably type.
    # Both the "industry" and "industryKeywords" inputs feed the same filter.
    industry_kw = csv_list('industryKeywords') or csv_list('industry')
    if industry_kw:
        filters['industryKeywords'] = industry_kw

    company_size = post.getlist('companyEmployeeSize')
    if company_size:
        filters['companyEmployeeSize'] = company_size

    revenue = post.getlist('revenue')
    if revenue:
        filters['revenue'] = revenue

    business_model = post.getlist('businessModel')
    if business_model:
        filters['businessModel'] = business_model

    filters['includeEmails'] = post.get('includeEmails') == 'true'

    try:
        total = int(post.get('totalResults', 1000))
        total = max(100, min(30000, total))
    except (ValueError, TypeError):
        total = 1000
    filters['totalResults'] = total

    return filters


@workspace_required
def advanced_search(request, workspace, membership):
    from .models import ApifySearch, ApifyRun, ApifySchedule
    from django.utils import timezone as _tz

    banner = None

    if request.method == 'POST':
        action    = request.POST.get('action', '')
        filters   = _parse_apify_filters(request.POST)
        search_id = request.POST.get('search_id') or None
        search    = None

        if search_id:
            try:
                search = ApifySearch.objects.get(pk=search_id, user=request.user, workspace=workspace)
                search.filters = filters
                name = request.POST.get('search_name', '').strip()
                if name:
                    search.name = name
                search.save()
            except ApifySearch.DoesNotExist:
                search = None

        if action in ('save', 'save_schedule', 'run') and not search:
            count = ApifySearch.objects.filter(user=request.user, workspace=workspace).count()
            name  = request.POST.get('search_name', '').strip() or f'Search {count + 1}'
            search = ApifySearch.objects.create(
                user=request.user, workspace=workspace, name=name, filters=filters,
            )

        if action == 'save_schedule' and search:
            cron_expr = request.POST.get('cron_expression', '0 9 * * 1').strip() or '0 9 * * 1'
            schedule, _ = ApifySchedule.objects.get_or_create(
                search=search,
                defaults={'user': request.user, 'cron_expression': cron_expr},
            )
            schedule.cron_expression = cron_expr
            schedule.is_active = True
            try:
                from croniter import croniter
                schedule.next_run_at = _dt.fromtimestamp(
                    croniter(cron_expr, _tz.now()).get_next(float), tz=_dt_tz.utc
                )
            except Exception:
                pass
            schedule.save()
            from django.shortcuts import redirect as _redir
            return _redir('advanced_search')

        if action == 'run' and search:
            try:
                from .services.apify import trigger_apify_run
                trigger_apify_run(search, request.user, 'manual', workspace)
                banner = 'Search started — leads will appear in Cold Leads automatically when complete.'
            except Exception as exc:
                banner = f'Failed to start search: {exc}'

        if action == 'save' and search:
            banner = 'Search saved.'

        from django.shortcuts import redirect as _redir
        if banner:
            request.session['advanced_search_banner'] = banner
        return _redir('advanced_search')

    banner = request.session.pop('advanced_search_banner', None)

    searches = (
        ApifySearch.objects
        .filter(user=request.user, workspace=workspace)
        .prefetch_related('runs', 'schedule')
        .order_by('-created_at')
    )

    # Build a map of search_pk → latest task_job dict for the JS progress system.
    # Only include jobs created in the last 7 days to keep the payload small.
    import json as _json
    from .models import TaskJob
    from datetime import timedelta
    from django.utils import timezone as _tz

    recent_jobs = (
        TaskJob.objects
        .filter(
            workspace=workspace,
            task_type='apify_import',
            created_at__gte=_tz.now() - timedelta(days=7),
        )
        .select_related('apify_run__search')
    )
    task_jobs_by_search = {}
    for tj in recent_jobs:
        if tj.apify_run and tj.apify_run.search_id:
            s_pk = tj.apify_run.search_id
            # Keep most recent job per search
            if s_pk not in task_jobs_by_search or tj.pk > task_jobs_by_search[s_pk]['id']:
                task_jobs_by_search[s_pk] = tj.as_dict()

    return render(request, 'crm/advanced_search.html', {
        'searches':               searches,
        'banner':                 banner,
        'seniority_options':      _SENIORITY_OPTIONS,
        'functional_options':     _FUNCTIONAL_OPTIONS,
        'company_size_options':   _COMPANY_SIZE_OPTIONS,
        'revenue_options':        _REVENUE_OPTIONS,
        'business_model_options': _BUSINESS_MODEL_OPTIONS,
        'email_status_options':   _EMAIL_STATUS_OPTIONS,
        'task_jobs_json':         _json.dumps(task_jobs_by_search),
    })


@csrf_exempt
def apify_webhook(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False}, status=405)

    secret = getattr(django_settings, 'APIFY_WEBHOOK_SECRET', '')
    if secret and request.GET.get('secret', '') != secret:
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    event_type = payload.get('eventType', '')
    resource   = payload.get('resource', {})
    run_id     = resource.get('id', '')
    dataset_id = resource.get('defaultDatasetId', '')

    from .models import ApifyRun, TaskJob
    from django.utils import timezone as _tz

    try:
        run = ApifyRun.objects.get(apify_run_id=run_id)
    except ApifyRun.DoesNotExist:
        return JsonResponse({'ok': True})

    if event_type == 'ACTOR.RUN.SUCCEEDED':
        run.apify_dataset_id = dataset_id
        run.status = 'RUNNING'
        run.save(update_fields=['apify_dataset_id', 'status'])
        job, created = TaskJob.objects.get_or_create(
            apify_run=run,
            defaults={'workspace': run.workspace, 'task_type': 'apify_import'},
        )
        if not created:
            return JsonResponse({'ok': True})
        import threading
        from crm.tasks import run_apify_import
        threading.Thread(target=run_apify_import, args=(run.pk, job.pk), daemon=True).start()
    elif event_type in ('ACTOR.RUN.FAILED', 'ACTOR.RUN.ABORTED'):
        run.status        = 'FAILED' if event_type == 'ACTOR.RUN.FAILED' else 'ABORTED'
        run.error_message = resource.get('statusMessage', '')
        run.completed_at  = _tz.now()
        run.save(update_fields=['status', 'error_message', 'completed_at'])

    return JsonResponse({'ok': True})


@login_required
def apify_run_status(request, run_id):
    from .models import ApifyRun
    try:
        run = ApifyRun.objects.get(apify_run_id=run_id, user=request.user)
    except ApifyRun.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    data = {
        'status':         run.status,
        'leads_imported': run.leads_imported,
        'error_message':  run.error_message,
    }
    try:
        data['task_job_id'] = run.task_job.pk
    except Exception:
        pass
    return JsonResponse(data)


@_api_workspace_required
@require_POST
def apify_trigger_run(request, search_id, workspace, membership):
    from .models import ApifySearch
    try:
        search = ApifySearch.objects.get(pk=search_id, user=request.user, workspace=workspace)
    except ApifySearch.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    try:
        from .services.apify import trigger_apify_run
        run = trigger_apify_run(search, request.user, 'manual', workspace)
        return JsonResponse({'ok': True, 'run_id': run.apify_run_id, 'run_pk': run.pk})
    except Exception as exc:
        return JsonResponse({'error': str(exc)}, status=500)


@_api_workspace_required
@require_POST
def apify_delete_search(request, search_id, workspace, membership):
    from .models import ApifySearch
    try:
        search = ApifySearch.objects.get(pk=search_id, user=request.user, workspace=workspace)
        search.delete()
        return JsonResponse({'ok': True})
    except ApifySearch.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)


@_api_workspace_required
@require_POST
def apify_toggle_schedule(request, search_id, workspace, membership):
    from .models import ApifySearch, ApifySchedule
    try:
        search   = ApifySearch.objects.get(pk=search_id, user=request.user, workspace=workspace)
        schedule = search.schedule
        schedule.is_active = not schedule.is_active
        schedule.save(update_fields=['is_active'])
        return JsonResponse({'ok': True, 'is_active': schedule.is_active})
    except (ApifySearch.DoesNotExist, ApifySchedule.DoesNotExist):
        return JsonResponse({'error': 'Not found'}, status=404)


# ── Drip Campaign API ─────────────────────────────────────────────────────────

@_api_workspace_required
@require_POST
def drip_generate(request, contact_id, workspace, membership):
    """Generate a pending DripEmail draft for a contact."""
    from .models import Contact, DripEmail
    from .drip import generate_drip_for_contact

    try:
        contact = Contact.objects.get(pk=contact_id, workspace=workspace)
    except Contact.DoesNotExist:
        return JsonResponse({'error': 'Contact not found'}, status=404)

    if contact.drip_sequence_stopped:
        return JsonResponse({'error': 'Drip sequence has been stopped for this contact'}, status=400)

    # Don't generate a second pending draft
    existing = contact.drip_emails.filter(status='pending').first()
    if existing:
        return JsonResponse({
            'ok': True,
            'drip_email': _serialize_drip(existing),
            'reused': True,
        })

    try:
        drip = generate_drip_for_contact(contact, workspace)
    except RuntimeError as e:
        return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'ok': True, 'drip_email': _serialize_drip(drip)})


@_api_workspace_required
@require_POST
def drip_save_edit(request, drip_pk, workspace, membership):
    """Save a human edit to a drip email draft. Updates the linked training record."""
    from .models import DripEmail, DripEditExample
    data = json.loads(request.body)

    try:
        drip = DripEmail.objects.select_related('contact').get(
            pk=drip_pk, contact__workspace=workspace,
        )
    except DripEmail.DoesNotExist:
        return JsonResponse({'error': 'Drip email not found'}, status=404)

    new_body    = (data.get('body') or '').strip()
    new_subject = (data.get('subject') or drip.subject).strip()

    if not new_body:
        return JsonResponse({'error': 'Body is required'}, status=400)

    original_body = drip.body
    drip.body    = new_body
    drip.subject = new_subject
    drip.status  = 'pending'
    drip.save(update_fields=['body', 'subject', 'status'])

    # Update linked training record if it exists; otherwise create a legacy one
    linked_example = drip.drip_edit_examples.first()
    if linked_example:
        linked_example.edited_body = new_body
        linked_example.save(update_fields=['edited_body'])
    elif new_body != original_body:
        # Legacy path: no linked example (e.g., record pre-dates this pipeline)
        DripEditExample.objects.create(
            workspace=workspace,
            drip_email=drip,
            contact=drip.contact,
            original_body=original_body,
            edited_body=new_body,
        )

    return JsonResponse({'ok': True, 'drip_email': _serialize_drip(drip)})


@_api_workspace_required
@require_POST
def drip_send(request, drip_pk, workspace, membership):
    """Approve and send a pending DripEmail."""
    from .models import DripEmail
    from .drip import send_drip_email

    try:
        drip = DripEmail.objects.select_related('contact').get(
            pk=drip_pk, contact__workspace=workspace,
        )
    except DripEmail.DoesNotExist:
        return JsonResponse({'error': 'Drip email not found'}, status=404)

    if drip.status == 'sent':
        return JsonResponse({'error': 'Already sent'}, status=400)

    try:
        send_drip_email(drip)
    except RuntimeError as e:
        return JsonResponse({'error': str(e)}, status=500)

    return JsonResponse({'ok': True, 'drip_email': _serialize_drip(drip)})


@_api_workspace_required
@require_POST
def drip_reject(request, drip_pk, workspace, membership):
    """Reject (discard) a pending DripEmail draft."""
    from .models import DripEmail

    try:
        drip = DripEmail.objects.select_related('contact').get(
            pk=drip_pk, contact__workspace=workspace,
        )
    except DripEmail.DoesNotExist:
        return JsonResponse({'error': 'Drip email not found'}, status=404)

    drip.status = 'rejected'
    drip.save(update_fields=['status'])
    return JsonResponse({'ok': True})


@_api_workspace_required
@require_POST
def drip_pause(request, contact_id, workspace, membership):
    """Toggle drip_paused on a contact."""
    from .models import Contact

    try:
        contact = Contact.objects.get(pk=contact_id, workspace=workspace)
    except Contact.DoesNotExist:
        return JsonResponse({'error': 'Contact not found'}, status=404)

    contact.drip_paused = not contact.drip_paused
    contact.save(update_fields=['drip_paused'])
    return JsonResponse({'ok': True, 'paused': contact.drip_paused})


@_api_workspace_required
def drip_list(request, contact_id, workspace, membership):
    """Return all drip emails for a contact."""
    from .models import Contact, DripEmail

    try:
        contact = Contact.objects.get(pk=contact_id, workspace=workspace)
    except Contact.DoesNotExist:
        return JsonResponse({'error': 'Contact not found'}, status=404)

    drips = contact.drip_emails.order_by('sequence_number', 'created_at')
    return JsonResponse({
        'drip_emails': [_serialize_drip(d) for d in drips],
        'drip_followups_sent':   contact.drip_followups_sent,
        'drip_sequence_stopped': contact.drip_sequence_stopped,
        'drip_paused':           contact.drip_paused,
    })


def _serialize_drip(drip):
    return {
        'id':              drip.pk,
        'sequence_number': drip.sequence_number,
        'subject':         drip.subject,
        'body':            drip.body,
        'status':          drip.status,
        'sent_at':         drip.sent_at.isoformat() if drip.sent_at else None,
        'created_at':      drip.created_at.isoformat() if drip.created_at else None,
    }


# ── Backup Outreach ───────────────────────────────────────────────────────────

@_api_workspace_required
@require_POST
def backup_outreach(request, workspace, membership):
    """
    Kick off a background task to send outreach to any contacts from the
    last Advanced Search import that were missed (imported but never emailed).
    Only one active backup-outreach job is allowed per workspace at a time.
    """
    from .models import TaskJob

    # Block if one is already running
    active = TaskJob.objects.filter(
        workspace=workspace,
        task_type='backup_outreach',
        status__in=['pending', 'running'],
    ).first()
    if active:
        return JsonResponse({'ok': False, 'job_id': active.pk, 'already_running': True})

    job = TaskJob.objects.create(
        workspace=workspace,
        task_type='backup_outreach',
    )
    import threading
    from crm.tasks import run_backup_outreach_task
    threading.Thread(target=run_backup_outreach_task, args=(workspace.pk, request.user.pk, job.pk), daemon=True).start()

    return JsonResponse({'ok': True, 'job_id': job.pk})


# ── Task Status API ───────────────────────────────────────────────────────────

@_api_workspace_required
def task_status(request, job_pk, workspace, membership):
    """Return current progress of a TaskJob as JSON (polled by the frontend)."""
    from .models import TaskJob
    try:
        job = TaskJob.objects.get(pk=job_pk, workspace=workspace)
    except TaskJob.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)
    return JsonResponse(job.as_dict())


# ── Training Data Admin API ───────────────────────────────────────────────────

@_api_workspace_required
def training_data_stats(request, workspace, membership):
    """GET — training data stats for this workspace (admin only)."""
    if not _is_admin(request, membership):
        return JsonResponse({'error': 'Admin access required'}, status=403)

    from django.db.models import Avg, Count
    from .models import DripEditExample

    qs = DripEditExample.objects.filter(workspace=workspace)

    total           = qs.count()
    high_quality    = qs.filter(is_high_quality=True).count()
    with_reply      = qs.filter(reply_received=True).count()
    unexported      = qs.filter(exported_at__isnull=True).count()
    avg_score_row   = qs.filter(outcome_score__isnull=False).aggregate(avg=Avg('outcome_score'))
    avg_score       = round(avg_score_row['avg'] or 0.0, 3)

    # Count per sequence_number
    by_seq_qs = (
        qs.filter(sequence_number__isnull=False)
        .values('sequence_number')
        .annotate(n=Count('id'))
        .order_by('sequence_number')
    )
    by_sequence = {str(row['sequence_number']): row['n'] for row in by_seq_qs}

    # Last export timestamp
    last_exported = (
        qs.filter(exported_at__isnull=False)
        .order_by('-exported_at')
        .values_list('exported_at', flat=True)
        .first()
    )

    cfg = HeatSettings.get_for_workspace(workspace)

    return JsonResponse({
        'total_examples':  total,
        'high_quality':    high_quality,
        'with_reply':      with_reply,
        'unexported':      unexported,
        'avg_outcome_score': avg_score,
        'by_sequence':     by_sequence,
        'last_export':     last_exported.isoformat() if last_exported else None,
        'drip_model_id':   cfg.drip_model_id or None,
    })


@_api_workspace_required
@require_POST
def training_data_flag(request, example_pk, workspace, membership):
    """POST — manually override is_high_quality on a DripEditExample (admin only)."""
    if not _is_admin(request, membership):
        return JsonResponse({'error': 'Admin access required'}, status=403)

    from .models import DripEditExample
    data = json.loads(request.body)

    try:
        example = DripEditExample.objects.get(pk=example_pk, workspace=workspace)
    except DripEditExample.DoesNotExist:
        return JsonResponse({'error': 'Example not found'}, status=404)

    example.is_high_quality = bool(data.get('is_high_quality', False))
    example.save(update_fields=['is_high_quality'])
    return JsonResponse({'ok': True})


@_api_workspace_required
@require_POST
def training_data_set_model_id(request, workspace, membership):
    """POST — save drip_model_id to HeatSettings (admin only)."""
    if not _is_admin(request, membership):
        return JsonResponse({'error': 'Admin access required'}, status=403)

    data = json.loads(request.body)
    cfg  = HeatSettings.get_for_workspace(workspace)
    cfg.drip_model_id = (data.get('drip_model_id') or '').strip()
    cfg.save(update_fields=['drip_model_id'])
    return JsonResponse({'ok': True})


# ── Contact Detail Page ───────────────────────────────────────────────────────

@workspace_required
def contact_detail(request, pk, workspace, membership):
    contact = get_object_or_404(Contact, pk=pk, workspace=workspace)
    cfg = HeatSettings.get_for_workspace(workspace)
    heat_score = calculate_score(contact, cfg)
    touchpoints = contact.touchpoints.all().order_by('-date', '-created_at')
    email_threads = contact.email_thread.order_by('-sent_at')[:50]
    parts = contact.name.split()
    initials = (parts[0][0] + parts[-1][0]).upper() if len(parts) > 1 else contact.name[:2].upper()

    return render(request, 'crm/contact_detail.html', {
        'contact':         contact,
        'heat_score':      heat_score,
        'touchpoints':     touchpoints,
        'email_threads':   email_threads,
        'outcome_choices': CALL_OUTCOME_CHOICES,
        'initials':        initials,
    })


@_api_workspace_required
@require_POST
def contact_toggle_called(request, pk, workspace, membership):
    contact = get_object_or_404(Contact, pk=pk, workspace=workspace)
    contact.called = not contact.called
    contact.save(update_fields=['called'])
    return JsonResponse({'ok': True, 'called': contact.called})


@_api_workspace_required
@require_POST
def contact_set_outcome(request, pk, workspace, membership):
    contact = get_object_or_404(Contact, pk=pk, workspace=workspace)
    data = json.loads(request.body)
    outcome = data.get('outcome', '')
    valid = {k for k, _ in CALL_OUTCOME_CHOICES}
    if outcome and outcome not in valid:
        return JsonResponse({'error': 'Invalid outcome'}, status=400)
    contact.call_outcome = outcome
    update_fields = ['call_outcome']

    called_updated = False
    if outcome and not contact.called:
        contact.called = True
        called_updated = True
        update_fields.append('called')

    stage_changed = False
    next_stage = _advance_stage(contact.stage, 'call', outcome) if outcome else None
    if next_stage:
        contact.stage = next_stage
        stage_changed = True
        update_fields.append('stage')

    contact.save(update_fields=update_fields)
    return JsonResponse({
        'ok':             True,
        'call_outcome':   contact.call_outcome,
        'called_updated': called_updated,
        'stage_changed':  stage_changed,
        'new_stage':      contact.stage,
        'new_stage_label': _STAGE_LABEL.get(contact.stage, '') if stage_changed else '',
    })


@_api_workspace_required
@require_POST
def contact_toggle_email_outreach(request, pk, workspace, membership):
    contact = get_object_or_404(Contact, pk=pk, workspace=workspace)
    contact.email_outreach_enabled = not contact.email_outreach_enabled
    contact.save(update_fields=['email_outreach_enabled'])
    return JsonResponse({'ok': True, 'email_outreach_enabled': contact.email_outreach_enabled})


@_api_workspace_required
@require_POST
def contact_save_financials(request, pk, workspace, membership):
    contact = get_object_or_404(Contact, pk=pk, workspace=workspace)
    data = json.loads(request.body)
    fields = ['revenue', 'ebitda', 'company_size', 'ownership_structure', 'reason_for_sale', 'causality_notes']
    for f in fields:
        if f in data:
            setattr(contact, f, data[f])
    contact.save(update_fields=fields)
    return JsonResponse({'ok': True})


@_api_workspace_required
@require_POST
def contact_save_call_notes(request, pk, workspace, membership):
    contact = get_object_or_404(Contact, pk=pk, workspace=workspace)
    data = json.loads(request.body)
    contact.call_notes = data.get('call_notes', '').strip()
    contact.save(update_fields=['call_notes'])
    return JsonResponse({'ok': True})


# ── Cold Lead List (search / filter / sort) ────────────────────────────────────

_HEAT_ORDER = Case(
    When(heat='active',  then=Value(5)),
    When(heat='warm',    then=Value(4)),
    When(heat='medium',  then=Value(3)),
    When(heat='cold',    then=Value(2)),
    When(heat='dormant', then=Value(1)),
    default=Value(0),
    output_field=IntegerField(),
)

_SORT_FIELD_MAP = {
    'name':         'name',
    'company':      'company',
    'location':     'location',
    'industry':     'industry',
    'called':       'called',
    'call_outcome': 'call_outcome',
    'created_at':   'created_at',
    'updated_at':   'updated_at',
    'heat':         'heat_order',
}

_FILTER_DB_FIELD = {
    'name':         'name',
    'email':        'email',
    'company':      'company',
    'role':         'role',
    'location':     'location',
    'industry':     'industry',
    'heat':         'heat',
    'called':       'called',
    'call_outcome': 'call_outcome',
    'phone':        'phone',
    'created_at':   'created_at',
    'updated_at':   'updated_at',
}

_QUICK_CHIPS = [
    ('ready_to_call',   '📞 Ready to Call'),
    ('hot_leads',       '🔥 Hot Leads'),
    ('responded',       '💬 Responded'),
    ('added_this_week', '📅 Added This Week'),
    ('not_contacted',   '📬 Not Yet Contacted'),
]


def _build_filter_q(field, op, val):
    """Return a Q object for one filter row, or None if invalid/unsupported."""
    from django.utils import timezone as tz
    db = _FILTER_DB_FIELD.get(field)
    if not db:
        return None
    try:
        if op == 'contains':
            return Q(**{f'{db}__icontains': val})
        if op == 'not_contains':
            return ~Q(**{f'{db}__icontains': val})
        if op == 'equals':
            return Q(**{f'{db}__iexact': val})
        if op == 'not_equals':
            return ~Q(**{f'{db}__iexact': val})
        if op == 'starts_with':
            return Q(**{f'{db}__istartswith': val})
        if op == 'ends_with':
            return Q(**{f'{db}__iendswith': val})
        if op == 'is_empty':
            return Q(**{f'{db}': ''}) | Q(**{f'{db}__isnull': True})
        if op == 'is_not_empty':
            return ~(Q(**{f'{db}': ''}) | Q(**{f'{db}__isnull': True}))
        if op == 'is':
            return Q(**{f'{db}': val})
        if op == 'is_not':
            return ~Q(**{f'{db}': val})
        if op == 'is_any_of':
            vals = [v.strip() for v in val.split(',') if v.strip()]
            return Q(**{f'{db}__in': vals}) if vals else None
        if op == 'is_none_of':
            vals = [v.strip() for v in val.split(',') if v.strip()]
            return ~Q(**{f'{db}__in': vals}) if vals else None
        if op == 'is_true':
            return Q(**{f'{db}': True})
        if op == 'is_false':
            return Q(**{f'{db}': False})
        # Date operators — use db field name so both created_at and updated_at work
        if op == 'is_date':
            return Q(**{f'{db}__date': val})
        if op == 'is_not_date':
            return ~Q(**{f'{db}__date': val})
        if op == 'is_before':
            return Q(**{f'{db}__date__lt': val})
        if op == 'is_after':
            return Q(**{f'{db}__date__gt': val})
        if op == 'is_on_or_before':
            return Q(**{f'{db}__date__lte': val})
        if op == 'is_on_or_after':
            return Q(**{f'{db}__date__gte': val})
        if op == 'is_between':
            parts = [v.strip() for v in val.split(',')]
            if len(parts) == 2 and parts[0] and parts[1]:
                return Q(**{f'{db}__date__gte': parts[0]}) & Q(**{f'{db}__date__lte': parts[1]})
        if op == 'in_last_x_days':
            days = int(val) if str(val).isdigit() else 7
            return Q(**{f'{db}__gte': tz.now() - datetime.timedelta(days=days)})
        if op == 'in_next_x_days':
            days = int(val) if str(val).isdigit() else 7
            now = tz.now()
            return Q(**{f'{db}__gte': now}) & Q(**{f'{db}__lte': now + datetime.timedelta(days=days)})
        if op == 'is_this_week':
            today = tz.now().date()
            week_start = today - datetime.timedelta(days=today.weekday())
            week_end   = week_start + datetime.timedelta(days=6)
            return Q(**{f'{db}__date__gte': week_start}) & Q(**{f'{db}__date__lte': week_end})
        if op == 'is_this_month':
            today = tz.now().date()
            return Q(**{f'{db}__year': today.year}) & Q(**{f'{db}__month': today.month})
        if op == 'has_no_value':
            return Q(**{f'{db}__isnull': True})
    except Exception:
        pass
    return None


@workspace_required
def cold_lead_list(request, workspace, membership):
    from django.core.paginator import Paginator
    from django.utils import timezone

    qs = (Contact.objects
          .filter(workspace=workspace, stage='cold_lead')
          .annotate(heat_order=_HEAT_ORDER))

    # ── Text search ──────────────────────────────────────────────────────────
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(company__icontains=q))

    # ── Quick chips ──────────────────────────────────────────────────────────
    chips = request.GET.getlist('chip')
    for chip in chips:
        if chip == 'ready_to_call':
            qs = qs.filter(phone__gt='', called=False)
        elif chip == 'hot_leads':
            qs = qs.filter(heat__in=['warm', 'active'])
        elif chip == 'responded':
            qs = qs.filter(call_outcome='interested')
        elif chip == 'added_this_week':
            qs = qs.filter(created_at__gte=timezone.now() - datetime.timedelta(days=7))
        elif chip == 'not_contacted':
            qs = qs.filter(called=False)

    # ── Filter builder rows ──────────────────────────────────────────────────
    filter_logic = request.GET.get('filter_logic', 'AND')
    filter_rows  = []
    conditions   = []
    for key in sorted((k for k in request.GET if re.match(r'^ff\d+$', k)),
                      key=lambda k: int(k[2:])):
        idx   = int(key[2:])
        field = request.GET.get(f'ff{idx}', '').strip()
        op    = request.GET.get(f'fo{idx}', '').strip()
        val   = request.GET.get(f'fv{idx}', '').strip()
        filter_rows.append({'field': field, 'op': op, 'val': val, 'idx': idx})
        if field and op:
            cond = _build_filter_q(field, op, val)
            if cond is not None:
                conditions.append(cond)

    if conditions:
        combined = conditions[0]
        for c in conditions[1:]:
            combined = combined | c if filter_logic == 'OR' else combined & c
        qs = qs.filter(combined)

    # ── Multi-level sort (s1/d1 … s5/d5); falls back to old sort/sort_dir ───
    sort_levels = []
    for i in range(1, 6):
        sf = request.GET.get(f's{i}', '').strip()
        sd = request.GET.get(f'd{i}', 'desc').strip()
        if sf and sf in _SORT_FIELD_MAP:
            sort_levels.append({'field': sf, 'dir': sd if sd in ('asc', 'desc') else 'desc'})
    if not sort_levels:
        # Backward compat with old single-level sort params
        old_field = request.GET.get('sort', 'heat')
        old_dir   = request.GET.get('sort_dir', 'desc')
        sort_levels = [{'field': old_field if old_field in _SORT_FIELD_MAP else 'heat',
                        'dir':   old_dir   if old_dir   in ('asc', 'desc') else 'desc'}]

    order_args = []
    for sl in sort_levels:
        db_field = _SORT_FIELD_MAP.get(sl['field'], 'heat_order')
        order_args.append(f"-{db_field}" if sl['dir'] == 'desc' else db_field)
    order_args.append('-created_at')
    qs = qs.order_by(*order_args)

    primary_sort_field = sort_levels[0]['field']
    primary_sort_dir   = sort_levels[0]['dir']

    # ── Active saved-filter pill ─────────────────────────────────────────────
    try:
        active_pill_id = int(request.GET.get('active_pill_id', ''))
    except (ValueError, TypeError):
        active_pill_id = None

    # ── Counts + pagination ──────────────────────────────────────────────────
    total    = Contact.objects.filter(workspace=workspace, stage='cold_lead').count()
    paginator = Paginator(qs, 100)
    page_num  = request.GET.get('page', 1)
    page_obj  = paginator.get_page(page_num)
    count     = qs.count()

    saved_filters = list(
        SavedFilter.objects
        .filter(workspace=workspace, user=request.user)
        .values('id', 'name', 'emoji', 'filter_state')
    )

    return render(request, 'crm/cold_lead_list.html', {
        'contacts':           page_obj,
        'page_obj':           page_obj,
        'total':              total,
        'count':              count,
        'q':                  q,
        'chips':              chips,
        'chips_json':         json.dumps(chips),
        'filter_rows_json':   json.dumps(filter_rows),
        'filter_logic':       filter_logic,
        'sort_levels_json':   json.dumps(sort_levels),
        'primary_sort_field': primary_sort_field,
        'primary_sort_dir':   primary_sort_dir,
        'saved_filters':      json.dumps(saved_filters),
        'active_pill_id':     active_pill_id,
        'quick_chips':        _QUICK_CHIPS,
        'heat_meta':          HEAT_META,
        'outcome_choices':    CALL_OUTCOME_CHOICES,
        'workspace':          workspace,
        'filters_active':     bool(q or chips or filter_rows),
    })


@_api_workspace_required
@require_POST
def saved_filter_save(request, workspace, membership):
    data      = json.loads(request.body)
    name      = data.get('name', '').strip()
    state     = data.get('state', {})
    emoji     = data.get('emoji', '').strip()[:8]
    update_id = data.get('update_id')

    if update_id:
        obj = get_object_or_404(SavedFilter, pk=update_id, workspace=workspace, user=request.user)
        obj.filter_state = state
        if emoji:
            obj.emoji = emoji
        obj.save(update_fields=['filter_state', 'emoji'])
        return JsonResponse({'id': obj.pk, 'name': obj.name, 'emoji': obj.emoji, 'created': False})

    if not name:
        return JsonResponse({'error': 'Name is required'}, status=400)
    if SavedFilter.objects.filter(workspace=workspace, user=request.user).count() >= 25:
        return JsonResponse({'error': 'Maximum 25 saved filters reached'}, status=400)
    obj, created = SavedFilter.objects.get_or_create(
        workspace=workspace, user=request.user, name=name,
        defaults={'filter_state': state, 'emoji': emoji},
    )
    if not created:
        obj.filter_state = state
        if emoji:
            obj.emoji = emoji
        obj.save(update_fields=['filter_state', 'emoji'])
    return JsonResponse({'id': obj.pk, 'name': obj.name, 'emoji': obj.emoji, 'created': created})


@_api_workspace_required
@require_POST
def saved_filter_delete(request, pk, workspace, membership):
    obj = get_object_or_404(SavedFilter, pk=pk, workspace=workspace, user=request.user)
    obj.delete()
    return JsonResponse({'ok': True})


@_api_workspace_required
@require_POST
def ai_contact_search(request, workspace, membership):
    import os as _os, anthropic as _anthropic

    body  = json.loads(request.body)
    query = (body.get('query') or '').strip()
    if not query:
        return JsonResponse({'error': 'Query is required'}, status=400)

    api_key = _os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return JsonResponse({'error': 'AI not configured'}, status=400)

    # ── Step 1: extract structured criteria via Haiku ─────────────────────────
    from datetime import date as _date
    current_year = _date.today().year

    system_prompt = (
        f"You are a CRM search assistant. Today is {current_year}. "
        "Extract structured search criteria from a natural language query about business contacts/companies.\n\n"
        "Return ONLY valid JSON (no markdown) with these fields (omit or use [] / null if not applicable):\n"
        "{\n"
        '  "industry_terms": [],       // industries or business types (include synonyms)\n'
        '  "location_terms": [],       // locations, regions, states, cities (expand abbreviations)\n'
        '  "company_terms": [],        // company name keywords\n'
        '  "role_terms": [],           // job titles or roles\n'
        '  "size_terms": [],           // company size descriptors\n'
        '  "org_type_terms": [],       // org type (nonprofit, LLC, franchise, etc)\n'
        '  "min_founded_year": null,   // integer or null\n'
        '  "max_founded_year": null,   // integer or null\n'
        '  "explanation": ""           // plain-English summary of the search\n'
        "}\n\n"
        "Examples:\n"
        "- \"HVAC companies in the southeast\" → industry_terms:[\"HVAC\",\"heating\",\"cooling\",\"air conditioning\",\"HVAC/R\"], "
        "location_terms:[\"southeast\",\"Georgia\",\"Florida\",\"Alabama\",\"South Carolina\",\"Tennessee\",\"Mississippi\",\"Louisiana\",\"North Carolina\"]\n"
        f"- \"Tire shops over 10 years old\" → industry_terms:[\"tire\",\"auto repair\",\"automotive\"], "
        f"max_founded_year:{current_year - 10}, explanation:\"Tire shops founded more than 10 years ago\"\n"
        "- \"CFOs at manufacturing companies\" → role_terms:[\"CFO\",\"Chief Financial Officer\"], industry_terms:[\"manufacturing\",\"production\",\"industrial\"]"
    )

    try:
        client = _anthropic.Anthropic(api_key=api_key)
        resp   = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=512,
            system=system_prompt,
            messages=[{'role': 'user', 'content': query}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith('```'):
            raw = '\n'.join(raw.split('\n')[1:]).rstrip('`').strip()
        criteria = json.loads(raw)
    except Exception as exc:
        logger.error('AI contact search extraction failed: %s', exc)
        return JsonResponse({'error': 'AI search failed — please try again'}, status=500)

    # ── Step 2: score all cold_lead contacts ─────────────────────────────────
    industry_terms = [t.lower() for t in (criteria.get('industry_terms')  or [])]
    location_terms = [t.lower() for t in (criteria.get('location_terms')  or [])]
    company_terms  = [t.lower() for t in (criteria.get('company_terms')   or [])]
    role_terms     = [t.lower() for t in (criteria.get('role_terms')      or [])]
    size_terms     = [t.lower() for t in (criteria.get('size_terms')      or [])]
    org_type_terms = [t.lower() for t in (criteria.get('org_type_terms')  or [])]
    min_yr         = criteria.get('min_founded_year')
    max_yr         = criteria.get('max_founded_year')

    def _hits(field_val, terms):
        if not field_val or not terms:
            return 0
        fv = field_val.lower()
        return sum(1 for t in terms if t in fv)

    contacts_qs = Contact.objects.filter(
        workspace=workspace, stage='cold_lead',
    ).values('pk', 'industry', 'location', 'company', 'role',
             'org_type', 'org_founded_year', 'company_size')

    matches = []
    for c in contacts_qs:
        score  = 0
        score += _hits(c['industry'],    industry_terms) * 3
        score += _hits(c['location'],    location_terms) * 2
        score += _hits(c['company'],     company_terms)  * 2
        score += _hits(c['role'],        role_terms)     * 1
        score += _hits(c['company_size'],size_terms)     * 1
        score += _hits(c['org_type'],    org_type_terms) * 1

        yr_str = (c['org_founded_year'] or '').strip()
        if yr_str.isdigit():
            yr = int(yr_str)
            if (min_yr and yr < min_yr) or (max_yr and yr > max_yr):
                score -= 3
            elif min_yr or max_yr:
                score += 2

        if score > 0:
            matches.append({'pk': c['pk'], 'score': score})

    matches.sort(key=lambda x: x['score'], reverse=True)

    return JsonResponse({
        'explanation': criteria.get('explanation') or query,
        'matches':     matches,
        'total':       len(matches),
    })
