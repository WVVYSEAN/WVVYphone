from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models

SOURCE_CHOICES = [
    ('referral',               'Referral'),
    ('inbound',                'Inbound'),
    ('event',                  'Event'),
    ('cold_research',          'Cold Research'),
    ('apify_advanced_search',  'Apify Advanced Search'),
]

STAGE_META = [
    ('cold_lead',      'Cold Lead',      'wvvy-badge wvvy-badge-muted'),
    ('warm_lead',      'Warm Lead',      'wvvy-badge wvvy-badge-amber'),
    ('discovery_call', 'Discovery Call', 'wvvy-badge wvvy-badge-cyan'),
    ('proposal',       'Proposal',       'wvvy-badge wvvy-badge-violet'),
    ('negotiation',    'Negotiation',    'wvvy-badge wvvy-badge-amber'),
    ('closed_won',     'Closed Won',     'wvvy-badge wvvy-badge-cyan'),
    ('closed_lost',    'Closed Lost',    'wvvy-badge wvvy-badge-rose'),
]
STAGE_CHOICES = [(k, l) for k, l, _ in STAGE_META]

TOUCHPOINT_CHOICES = [
    ('email',          'Email Sent'),
    ('call',           'Call'),
    ('voicemail',      'Voicemail'),
    ('text',           'Text'),
    ('meeting',        'Meeting'),
    ('event',          'Event'),
    ('linkedin',       'LinkedIn Interaction'),
    ('proposal',       'Proposal Sent'),
    ('product_launch', 'Product Launch'),
    ('other',          'Other'),
]

CALL_OUTCOME_CHOICES = [
    ('interested',     'Interested'),
    ('not_now',        'Not Now'),
    ('not_interested', 'Not Interested'),
    ('booked',         'Discovery Booked'),
    ('no_answer',      'No Answer'),
]

HEAT_META = [
    ('cold',    'Cold',           'wvvy-badge wvvy-badge-muted'),
    ('medium',  'Medium',         'wvvy-badge wvvy-badge-amber'),
    ('warm',    'Warm',           'wvvy-badge wvvy-badge-rose'),
    ('active',  'Active Client',  'wvvy-badge wvvy-badge-cyan'),
    ('dormant', 'Dormant Client', 'wvvy-badge wvvy-badge-muted'),
]
HEAT_CHOICES = [(k, l) for k, l, _ in HEAT_META]

HEAT_BADGE = {k: badge for k, _, badge in HEAT_META}


class Workspace(models.Model):
    name       = models.CharField(max_length=200)
    logo       = models.TextField(blank=True, default='')
    owner      = models.ForeignKey(
                     'auth.User', on_delete=models.PROTECT,
                     related_name='owned_workspaces')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


WORKSPACE_ROLE_CHOICES = [
    ('owner',  'Owner'),
    ('admin',  'Admin'),
    ('member', 'Member'),
]


class WorkspaceMembership(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE,
                                  related_name='memberships')
    user      = models.ForeignKey('auth.User', on_delete=models.CASCADE,
                                  related_name='workspace_memberships')
    role      = models.CharField(max_length=20, choices=WORKSPACE_ROLE_CHOICES,
                                 default='member')
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('workspace', 'user')]

    def __str__(self):
        return f'{self.user.email} → {self.workspace.name} ({self.role})'


INDUSTRY_LIST = [
    'Technology / SaaS',
    'E-commerce / DTC',
    'Financial Services',
    'Healthcare',
    'Media & Entertainment',
    'Marketing & Advertising',
    'Real Estate',
    'Education',
    'Manufacturing',
    'Retail',
    'Food & Beverage',
    'Fashion & Apparel',
    'Travel & Hospitality',
    'Non-Profit',
    'Legal',
    'Construction',
    'Sports & Fitness',
    'Beauty & Wellness',
    'Automotive',
    'Consumer Goods',
]


class HeatSettings(models.Model):
    workspace = models.OneToOneField(
                    Workspace, on_delete=models.CASCADE,
                    related_name='heat_settings', null=True, blank=True)

    # Signal points
    pts_ideal_industry = models.IntegerField(default=15)
    pts_raised_funding = models.IntegerField(default=10)
    pts_product_launch = models.IntegerField(default=10)
    pts_referral       = models.IntegerField(default=20)
    pts_email_opened   = models.IntegerField(default=5)
    pts_responded      = models.IntegerField(default=20)
    pts_meeting_booked = models.IntegerField(default=30)

    # Upper-bound thresholds (inclusive) — active = above thresh_warm
    thresh_cold   = models.IntegerField(default=25)
    thresh_medium = models.IntegerField(default=50)
    thresh_warm   = models.IntegerField(default=75)

    # JSON list of selected industry strings
    ideal_industries = models.TextField(default='[]')

    # Resend email integration
    resend_api_key        = models.CharField(max_length=500, blank=True)
    resend_from_email     = models.CharField(max_length=200, blank=True,
                               help_text='e.g. "WVVYphone <you@yourdomain.com>"')
    resend_webhook_secret = models.CharField(max_length=500, blank=True)

    # Reply tracking
    reply_to_domain = models.CharField(max_length=200, blank=True,
                          help_text='Domain used for reply-to addresses, e.g. yourdomain.com')

    # Cold outreach email template
    outreach_enabled = models.BooleanField(default=False)
    outreach_subject = models.CharField(max_length=500, blank=True)
    outreach_body    = models.TextField(blank=True)

    # Email signature (used via {{signature}} variable in templates)
    signature = models.TextField(blank=True)

    # AI review mode — queue drafts instead of auto-sending
    ai_review_mode = models.BooleanField(default=False, help_text='Hold AI replies for human review before sending')

    calendar_booking_url  = models.CharField(max_length=1000, blank=True, verbose_name='Calendar Link',
                               help_text='Booking link sent by the AI in emails (e.g. Calendly, Cal.com)')

    # Drip campaign config
    drip_interval_days  = models.IntegerField(default=3,
                              help_text='Days between each drip email')
    drip_max_followups  = models.IntegerField(default=5,
                              help_text='Maximum drip emails to send per contact')

    # Fine-tuning / training data
    drip_model_id            = models.CharField(
                                   max_length=255, blank=True, default='',
                                   help_text='Fine-tuned OpenAI model ID. Leave blank to use Claude.')
    training_data_min_quality = models.FloatField(
                                   default=0.5,
                                   help_text='Minimum outcome_score to include in JSONL exports.')

    class Meta:
        verbose_name = 'Heat Settings'
        verbose_name_plural = 'Heat Settings'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def get_for_workspace(cls, workspace):
        obj, _ = cls.objects.get_or_create(workspace=workspace)
        return obj

    def get_ideal_industries(self):
        import json
        try:
            return json.loads(self.ideal_industries)
        except Exception:
            return []

    def heat_from_score(self, score):
        if score <= self.thresh_cold:
            return 'cold'
        elif score <= self.thresh_medium:
            return 'medium'
        elif score <= self.thresh_warm:
            return 'warm'
        else:
            return 'active'


def calculate_score(obj, settings=None):
    """Return 0-100 heat score for a Contact or Company."""
    if settings is None:
        settings = HeatSettings.get()

    score = 0
    tps = obj.touchpoints.all()
    tp_types = {tp.touchpoint_type for tp in tps}

    # Ideal industry
    ideal = settings.get_ideal_industries()
    industry = getattr(obj, 'industry', '')
    if ideal and industry and industry in ideal:
        score += settings.pts_ideal_industry

    # Raised funding (Company only — skip if attribute missing)
    funding = getattr(obj, 'funding_stage', '')
    if funding and funding.lower() not in ('', 'bootstrapped'):
        score += settings.pts_raised_funding

    # Product launch touchpoint
    if 'product_launch' in tp_types:
        score += settings.pts_product_launch

    # Referral source (Contact only)
    if getattr(obj, 'source', '') == 'referral':
        score += settings.pts_referral

    # Email touchpoint
    if 'email' in tp_types:
        score += settings.pts_email_opened

    # Responded to outreach (linkedin touchpoint)
    if 'linkedin' in tp_types:
        score += settings.pts_responded

    # Meeting booked
    if 'meeting' in tp_types:
        score += settings.pts_meeting_booked

    return min(score, 100)


def auto_heat(obj, settings=None):
    """Return the auto-calculated heat key for an object."""
    if settings is None:
        settings = HeatSettings.get()
    return settings.heat_from_score(calculate_score(obj, settings))


# Backward-compat alias
calculate_heat = auto_heat


class Contact(models.Model):
    workspace          = models.ForeignKey(Workspace, on_delete=models.CASCADE,
                             related_name='contacts', null=True, blank=True)
    stage              = models.CharField(max_length=20, choices=STAGE_CHOICES, default='cold_lead')
    name               = models.CharField(max_length=200)
    email              = models.EmailField(blank=True)
    phone              = models.CharField(max_length=50, blank=True, default='')
    company            = models.CharField(max_length=200, blank=True)
    company_domain     = models.CharField(max_length=200, blank=True, default='')
    role               = models.CharField(max_length=200, blank=True, verbose_name='Role / Title')
    linkedin           = models.URLField(blank=True, verbose_name='LinkedIn')
    location           = models.CharField(max_length=200, blank=True)
    industry           = models.CharField(max_length=200, blank=True)
    source             = models.CharField(max_length=25, choices=SOURCE_CHOICES, blank=True)
    email_status       = models.CharField(max_length=100, blank=True, default='', help_text='ZeroBounce email verification status')
    relationship_owner = models.CharField(max_length=200, blank=True)
    notes              = models.TextField(blank=True)
    heat               = models.CharField(max_length=20, choices=HEAT_CHOICES, default='cold')
    heat_override      = models.BooleanField(default=False)
    ai_managed         = models.BooleanField(default=True, help_text='Auto-reply with AI when a reply is received')
    last_message_id    = models.CharField(max_length=500, blank=True, help_text='Message-ID of last outbound email (for threading)')
    # AI follow-up sequence tracking
    timezone           = models.CharField(max_length=100, blank=True, default='UTC', help_text='IANA timezone for scheduling follow-ups (e.g. America/New_York)')
    follow_up_count    = models.IntegerField(default=0, help_text='Number of AI emails sent to this contact')
    last_follow_up_at  = models.DateTimeField(null=True, blank=True, help_text='When the last AI email was sent')
    needs_attention    = models.BooleanField(default=False, help_text='Lead expressed interest — needs a human response')
    sequence_stopped   = models.BooleanField(default=False, help_text='AI follow-up sequence is stopped for this contact')
    # Drip campaign tracking
    drip_followups_sent   = models.IntegerField(default=0, help_text='Number of drip emails sent to this contact')
    drip_sequence_stopped = models.BooleanField(default=False, help_text='Drip sequence permanently stopped (replied, unsubscribed, etc.)')
    drip_paused           = models.BooleanField(default=True, help_text='Drip sequence temporarily paused')
    # Phone-first workflow fields
    called               = models.BooleanField(default=False)
    call_outcome         = models.CharField(max_length=20, blank=True, default='', choices=CALL_OUTCOME_CHOICES)
    email_outreach_enabled = models.BooleanField(default=False)
    # Company profile fields (populated from Advanced Search import)
    org_type             = models.CharField(max_length=200, blank=True, default='')
    org_founded_year     = models.CharField(max_length=20,  blank=True, default='')
    org_revenue          = models.CharField(max_length=200, blank=True, default='')
    connections          = models.CharField(max_length=50,  blank=True, default='')
    # Financial / PE matching fields
    revenue              = models.CharField(max_length=100, blank=True, default='')
    ebitda               = models.CharField(max_length=100, blank=True, default='')
    company_size         = models.CharField(max_length=100, blank=True, default='')
    ownership_structure  = models.CharField(max_length=200, blank=True, default='')
    reason_for_sale      = models.CharField(max_length=200, blank=True, default='')
    causality_notes      = models.TextField(blank=True, default='')
    call_notes           = models.TextField(blank=True, default='')
    created_at         = models.DateTimeField(auto_now_add=True)
    updated_at         = models.DateTimeField(auto_now=True)
    touchpoints        = GenericRelation('TouchPoint')

    class Meta:
        ordering = ['-created_at']

    @property
    def heat_badge(self):
        return HEAT_BADGE.get(self.heat, 'bg-slate-100 text-slate-500')

    def __str__(self):
        return self.name + (f' ({self.company})' if self.company else '')


class Company(models.Model):
    workspace            = models.ForeignKey(Workspace, on_delete=models.CASCADE,
                               related_name='crm_companies', null=True, blank=True)
    stage                = models.CharField(max_length=20, choices=STAGE_CHOICES, default='cold_lead')
    company_name         = models.CharField(max_length=200)
    website              = models.URLField(blank=True)
    industry             = models.CharField(max_length=200, blank=True)
    size                 = models.CharField(max_length=200, blank=True, help_text='Revenue or employee count')
    funding_stage        = models.CharField(max_length=100, blank=True)
    product_category     = models.CharField(max_length=200, blank=True)
    last_funding_date    = models.DateField(null=True, blank=True)
    hq_location          = models.CharField(max_length=200, blank=True, verbose_name='HQ Location')
    agency_relationships = models.TextField(blank=True)
    notes                = models.TextField(blank=True)
    heat                 = models.CharField(max_length=20, choices=HEAT_CHOICES, default='cold')
    heat_override        = models.BooleanField(default=False)
    created_at           = models.DateTimeField(auto_now_add=True)
    touchpoints          = GenericRelation('TouchPoint')

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Companies'

    @property
    def heat_badge(self):
        return HEAT_BADGE.get(self.heat, 'bg-slate-100 text-slate-500')

    def __str__(self):
        return self.company_name


OPP_STAGE_CHOICES = [
    ('prospect',    'Prospect'),
    ('proposal',    'Proposal Sent'),
    ('negotiation', 'In Negotiation'),
    ('closed_won',  'Closed Won'),
    ('closed_lost', 'Closed Lost'),
]

SERVICE_CHOICES = [
    ('branding',  'Branding'),
    ('site',      'Website'),
    ('gtm',       'GTM Strategy'),
    ('packaging', 'Packaging'),
    ('social',    'Social Media'),
    ('email',     'Email Marketing'),
    ('other',     'Other'),
]


class Opportunity(models.Model):
    workspace         = models.ForeignKey(Workspace, on_delete=models.CASCADE,
                            related_name='opportunities', null=True, blank=True)
    company           = models.CharField(max_length=200)
    contact           = models.CharField(max_length=200, blank=True)
    estimated_value   = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    service_needed    = models.CharField(max_length=20, choices=SERVICE_CHOICES, blank=True)
    stage             = models.CharField(max_length=20, choices=OPP_STAGE_CHOICES, default='prospect')
    probability       = models.PositiveIntegerField(default=0, help_text='0–100%')
    expected_timeline = models.CharField(max_length=200, blank=True)
    notes             = models.TextField(blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-estimated_value']
        verbose_name_plural = 'Opportunities'

    def __str__(self):
        return f"{self.company} — {self.get_service_needed_display()}"


class TouchPoint(models.Model):
    # Generic link — can attach to Contact or Company
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id    = models.PositiveIntegerField()
    record       = GenericForeignKey('content_type', 'object_id')

    touchpoint_type = models.CharField(max_length=20, choices=TOUCHPOINT_CHOICES)
    date            = models.DateField()
    summary         = models.CharField(max_length=500, blank=True)
    notes           = models.TextField(blank=True)
    outcome         = models.CharField(max_length=20, blank=True, default='', choices=CALL_OUTCOME_CHOICES)
    logged_by       = models.CharField(max_length=200, blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f"{self.get_touchpoint_type_display()} — {self.date}"


class InvitedEmail(models.Model):
    email      = models.EmailField(unique=True)
    invited_at = models.DateTimeField(auto_now_add=True)
    # Workspace and role are set when an admin invites someone so that
    # the membership can be created automatically on their first login.
    workspace  = models.ForeignKey(
                     'Workspace', on_delete=models.SET_NULL,
                     null=True, blank=True, related_name='pending_invites')
    role       = models.CharField(max_length=20, default='member')

    class Meta:
        verbose_name = 'Invited Email'
        verbose_name_plural = 'Invited Emails'

    def __str__(self):
        return self.email




class UserProfile(models.Model):
    user             = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    from_email       = models.CharField(max_length=200, blank=True,
                           help_text='e.g. "Your Name <you@yourdomain.com>"')
    outreach_enabled = models.BooleanField(default=True)
    outreach_subject = models.CharField(max_length=500, blank=True)
    outreach_body    = models.TextField(blank=True)

    @classmethod
    def get_for_user(cls, user):
        obj, _ = cls.objects.get_or_create(user=user)
        return obj

    def __str__(self):
        return f'Profile({self.user.email})'


class EmailThread(models.Model):
    DIRECTION_CHOICES = [('outbound', 'Outbound'), ('inbound', 'Inbound')]

    contact     = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name='email_thread')
    message_id  = models.CharField(max_length=500, blank=True)
    in_reply_to = models.CharField(max_length=500, blank=True)
    direction   = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    subject     = models.CharField(max_length=500, blank=True)
    body        = models.TextField()
    flagged     = models.BooleanField(default=False, help_text='Flagged for human review')
    sent_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sent_at']

    def __str__(self):
        return f'{self.get_direction_display()} — {self.contact} — {self.sent_at:%Y-%m-%d %H:%M}'


class AICallLog(models.Model):
    contact       = models.ForeignKey(Contact, on_delete=models.CASCADE,
                        related_name='ai_logs', null=True, blank=True)
    prompt        = models.TextField()
    response      = models.TextField()
    input_tokens  = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    flagged       = models.BooleanField(default=False, help_text='Flagged — reply was NOT sent')
    # Approval queue
    STATUS_CHOICES = [
        ('auto_sent', 'Auto Sent'),
        ('pending',   'Pending Review'),
        ('approved',  'Approved'),
        ('edited',    'Edited & Sent'),
        ('rejected',  'Rejected'),
    ]
    status               = models.CharField(max_length=20, choices=STATUS_CHOICES, default='auto_sent')
    edited_response      = models.TextField(blank=True, help_text='What was actually sent, if edited before approval')
    draft_subject        = models.CharField(max_length=500, blank=True)
    draft_inbound_msg_id = models.CharField(max_length=500, blank=True, help_text='Lead Message-ID for threading')
    draft_in_reply_to    = models.CharField(max_length=500, blank=True, help_text='In-Reply-To header value')
    draft_is_followup    = models.BooleanField(default=False)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        flag = ' ⚠' if self.flagged else ''
        return f'AICallLog({self.contact}{flag}, {self.created_at:%Y-%m-%d %H:%M})'


class EmailTemplate(models.Model):
    workspace  = models.ForeignKey(Workspace, on_delete=models.CASCADE,
                     related_name='email_templates')
    name       = models.CharField(max_length=200)
    subject    = models.CharField(max_length=500, blank=True)
    body       = models.TextField(blank=True)   # HTML or plain text
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_default', 'name']
        verbose_name = 'Email Template'

    def __str__(self):
        return self.name


class OutreachAttachment(models.Model):
    """
    Files attached to all outgoing outreach for a workspace.
    Applied to: initial outreach (when no named template is used) + all drip emails.
    """
    workspace    = models.ForeignKey(Workspace, on_delete=models.CASCADE,
                       related_name='outreach_attachments')
    filename     = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    file_size    = models.IntegerField()            # bytes
    file_data    = models.BinaryField()
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['filename']

    def __str__(self):
        return self.filename


class EmailTemplateAttachment(models.Model):
    """Files attached to a specific named EmailTemplate."""
    email_template = models.ForeignKey(EmailTemplate, on_delete=models.CASCADE,
                         related_name='attachments')
    filename       = models.CharField(max_length=255)
    content_type   = models.CharField(max_length=100)
    file_size      = models.IntegerField()
    file_data      = models.BinaryField()
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['filename']

    def __str__(self):
        return self.filename


class EmailImage(models.Model):
    workspace   = models.ForeignKey(Workspace, on_delete=models.CASCADE,
                      related_name='email_images')
    name        = models.CharField(max_length=200, blank=True)
    image       = models.ImageField(upload_to='email_images/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name or str(self.image)


# ── Apify Advanced Search ─────────────────────────────────────────────────────

class ApifySearch(models.Model):
    user       = models.ForeignKey('auth.User', on_delete=models.CASCADE,
                     related_name='apify_searches')
    workspace  = models.ForeignKey(Workspace, on_delete=models.CASCADE,
                     related_name='apify_searches', null=True, blank=True)
    name       = models.CharField(max_length=200, blank=True)
    filters    = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name or f'Search #{self.pk}'

    def filters_summary(self):
        f = self.filters
        parts = []
        if f.get('personTitle'):
            titles = f['personTitle'][:2]
            parts.append(', '.join(titles))
        if f.get('seniority'):
            parts.append(f"{len(f['seniority'])} seniorities")
        if f.get('companyDomain'):
            parts.append('domains: ' + ', '.join(f['companyDomain'][:2]))
        if f.get('industry'):
            parts.append('industry: ' + ', '.join(f['industry'][:2]))
        if f.get('totalResults'):
            parts.append(f"{f['totalResults']} leads")
        return ' · '.join(parts) if parts else 'All leads'


APIFY_RUN_STATUS_CHOICES = [
    ('PENDING',   'Pending'),
    ('RUNNING',   'Running'),
    ('SUCCEEDED', 'Succeeded'),
    ('FAILED',    'Failed'),
    ('ABORTED',   'Aborted'),
]


class ApifyRun(models.Model):
    search           = models.ForeignKey(ApifySearch, on_delete=models.SET_NULL,
                           null=True, blank=True, related_name='runs')
    user             = models.ForeignKey('auth.User', on_delete=models.CASCADE,
                           related_name='apify_runs')
    workspace        = models.ForeignKey(Workspace, on_delete=models.CASCADE,
                           related_name='apify_runs', null=True, blank=True)
    apify_run_id     = models.CharField(max_length=200, unique=True)
    apify_dataset_id = models.CharField(max_length=200, blank=True)
    status           = models.CharField(max_length=20, choices=APIFY_RUN_STATUS_CHOICES,
                           default='PENDING')
    leads_imported   = models.IntegerField(default=0)
    triggered_by     = models.CharField(max_length=20, default='manual')
    started_at       = models.DateTimeField(auto_now_add=True)
    completed_at     = models.DateTimeField(null=True, blank=True)
    error_message    = models.TextField(blank=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f'ApifyRun({self.apify_run_id}, {self.status})'


class ApifySchedule(models.Model):
    search          = models.OneToOneField(ApifySearch, on_delete=models.CASCADE,
                          related_name='schedule')
    user            = models.ForeignKey('auth.User', on_delete=models.CASCADE,
                          related_name='apify_schedules')
    cron_expression = models.CharField(max_length=100, default='0 9 * * 1')
    is_active       = models.BooleanField(default=True)
    last_run_at     = models.DateTimeField(null=True, blank=True)
    next_run_at     = models.DateTimeField(null=True, blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Schedule({self.search}, {self.cron_expression})'


# ── Task Queue ───────────────────────────────────────────────────────────────

class TaskJob(models.Model):
    """Tracks the progress of a long-running background task (Celery)."""

    TASK_TYPES = [
        ('apify_import',    'Apify Import & Email'),
        ('backup_outreach', 'Backup Outreach'),
    ]
    STATUS_CHOICES = [
        ('pending',   'Pending'),
        ('running',   'Running'),
        ('succeeded', 'Succeeded'),
        ('failed',    'Failed'),
    ]

    workspace      = models.ForeignKey(Workspace, on_delete=models.CASCADE,
                         related_name='task_jobs')
    task_type      = models.CharField(max_length=50, choices=TASK_TYPES)
    # Linked to the Apify run that triggered this job (null for backup_outreach)
    apify_run      = models.OneToOneField(
                         'ApifyRun', on_delete=models.SET_NULL,
                         null=True, blank=True, related_name='task_job')
    celery_task_id = models.CharField(max_length=200, blank=True)
    status         = models.CharField(max_length=20, choices=STATUS_CHOICES,
                         default='pending')
    phase          = models.CharField(max_length=20, blank=True)  # 'importing' | 'emailing'

    # Phase 1 — importing leads from Apify dataset (+ ZeroBounce email clean)
    leads_total    = models.IntegerField(default=0)
    leads_imported = models.IntegerField(default=0)

    # Phase 2 — sending outreach emails
    emails_total   = models.IntegerField(default=0)
    emails_sent    = models.IntegerField(default=0)
    emails_skipped = models.IntegerField(default=0)

    error_message  = models.TextField(blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)
    completed_at   = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'TaskJob({self.task_type}, {self.status})'

    def as_dict(self):
        return {
            'id':              self.pk,
            'task_type':       self.task_type,
            'status':          self.status,
            'phase':           self.phase,
            'leads_total':     self.leads_total,
            'leads_imported':  self.leads_imported,
            'emails_total':    self.emails_total,
            'emails_sent':     self.emails_sent,
            'emails_skipped':  self.emails_skipped,
            'error_message':   self.error_message,
            'completed_at':    self.completed_at.isoformat() if self.completed_at else None,
        }


# ── Drip Campaign ─────────────────────────────────────────────────────────────

class DripEmail(models.Model):
    STATUS_CHOICES = [
        ('pending',  'Pending Review'),
        ('approved', 'Approved'),
        ('sent',     'Sent'),
        ('rejected', 'Rejected'),
    ]

    contact         = models.ForeignKey(Contact, on_delete=models.CASCADE,
                          related_name='drip_emails')
    sequence_number = models.IntegerField(default=1)
    subject         = models.CharField(max_length=500)
    body            = models.TextField()
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    scheduled_for   = models.DateTimeField(null=True, blank=True)
    sent_at         = models.DateTimeField(null=True, blank=True)
    ai_call_log     = models.ForeignKey(AICallLog, on_delete=models.SET_NULL,
                          null=True, blank=True, related_name='drip_emails')
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sequence_number', 'created_at']

    def __str__(self):
        return f'DripEmail #{self.sequence_number} → {self.contact} ({self.status})'


class DripEditExample(models.Model):
    """
    Training data record for each AI-generated drip email.
    Created at generation time; updated when the user edits or outcome is known.
    """
    workspace     = models.ForeignKey(Workspace, on_delete=models.CASCADE,
                        related_name='drip_edit_examples')
    # FK links (null for legacy records created before this schema)
    drip_email    = models.ForeignKey(
                        'DripEmail', on_delete=models.SET_NULL,
                        null=True, blank=True, related_name='drip_edit_examples')
    contact       = models.ForeignKey(
                        'Contact', on_delete=models.SET_NULL,
                        null=True, blank=True, related_name='drip_edit_examples')

    # Draft content
    original_body = models.TextField()
    edited_body   = models.TextField()

    # Prompt capture — filled at generation time
    full_system_prompt = models.TextField(blank=True, default='')
    full_user_prompt   = models.TextField(blank=True, default='')
    ai_raw_response    = models.TextField(blank=True, default='')
    model_used         = models.CharField(max_length=100, blank=True, default='')
    sequence_number    = models.PositiveIntegerField(null=True, blank=True)
    contact_industry   = models.CharField(max_length=255, blank=True, default='')

    # Outcome tracking — filled when contact replies
    reply_received    = models.BooleanField(default=False)
    reply_received_at = models.DateTimeField(null=True, blank=True)
    reply_intent      = models.CharField(max_length=50, blank=True, default='')

    # Quality scoring
    outcome_score  = models.FloatField(null=True, blank=True)
    is_high_quality = models.BooleanField(default=False)

    # Export tracking
    exported_at      = models.DateTimeField(null=True, blank=True)
    export_batch_id  = models.CharField(max_length=100, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'DripEditExample({self.workspace}, {self.created_at:%Y-%m-%d})'


class SavedFilter(models.Model):
    """Per-user saved filter/sort preset for the Cold Lead list."""
    workspace    = models.ForeignKey(Workspace, on_delete=models.CASCADE,
                       related_name='saved_filters')
    user         = models.ForeignKey('auth.User', on_delete=models.CASCADE,
                       related_name='saved_filters')
    name         = models.CharField(max_length=200)
    emoji        = models.CharField(max_length=8, blank=True, default='')
    filter_state = models.JSONField(default=dict)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        unique_together = [('workspace', 'user', 'name')]

    def __str__(self):
        return f'{self.name} ({self.user.email})'
