# Idempotent GlitchTip bootstrap: superuser, organization, team, project and project key
# from environment variables (see .env.example, section GlitchTip bootstrap).
# Runs inside the GlitchTip image (service glitchtip-bootstrap in docker-compose.obs.yml):
#   ./manage.py shell -c "exec(open('/bootstrap/bootstrap.py').read())"
# Re-running never duplicates objects and never changes the password of an existing user.
# Output: one marker line `GLITCHTIP_BOOTSTRAP_OK project_id=<id> public_key=<hex>`
# (the public key is part of the DSN and is not a secret). Passwords are never printed.
import os
import sys
import uuid

from allauth.account.models import EmailAddress
from apps.organizations_ext.models import Organization, OrganizationUser, OrganizationUserRole
from apps.projects.models import Project, ProjectKey
from apps.teams.models import Team
from django.contrib.auth import get_user_model
from django.db import transaction


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"GLITCHTIP_BOOTSTRAP_ERROR {name} is empty", file=sys.stderr)
        sys.exit(1)
    return value


def log(msg: str) -> None:
    print(f"glitchtip-bootstrap: {msg}")


email = env("GLITCHTIP_ADMIN_EMAIL").lower()
password = env("GLITCHTIP_ADMIN_PASSWORD")
org_slug = env("GLITCHTIP_ORG_SLUG")
org_name = env("GLITCHTIP_ORG_NAME")
team_slug = env("GLITCHTIP_TEAM_SLUG")
project_name = env("GLITCHTIP_PROJECT_NAME")
project_platform = env("GLITCHTIP_PROJECT_PLATFORM")
try:
    public_key = uuid.UUID(env("GLITCHTIP_PROJECT_PUBLIC_KEY"))
except ValueError:
    print("GLITCHTIP_BOOTSTRAP_ERROR GLITCHTIP_PROJECT_PUBLIC_KEY is not a UUID", file=sys.stderr)
    sys.exit(1)

User = get_user_model()

with transaction.atomic():
    # --- superuser (password is set only on creation) ---
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.create_superuser(email=email, password=password)
        log(f"user created id={user.pk}")
    else:
        changed = [f for f in ("is_superuser", "is_staff", "is_active") if not getattr(user, f)]
        for f in changed:
            setattr(user, f, True)
        if changed:
            user.save(update_fields=changed)
        log(f"user exists id={user.pk}" + (f" fixed={','.join(changed)}" if changed else ""))
    # allauth: mark the e-mail as verified and primary (login by e-mail, no confirmation mail)
    address, _ = EmailAddress.objects.get_or_create(
        user=user, email=email, defaults={"verified": True, "primary": True}
    )
    if not (address.verified and address.primary):
        address.verified = True
        address.primary = True
        address.save(update_fields=["verified", "primary"])

    # --- organization ---
    org, created = Organization.objects.get_or_create(slug=org_slug, defaults={"name": org_name})
    log(f"organization {'created' if created else 'exists'} id={org.pk} slug={org.slug}")
    org_user = OrganizationUser.objects.filter(organization=org, user=user).first()
    if org_user is None:
        org_user = org.add_user(user, OrganizationUserRole.OWNER)
        log(f"user added to organization as OWNER (org_user id={org_user.pk})")

    # --- team ---
    team, created = Team.objects.get_or_create(organization=org, slug=team_slug)
    log(f"team {'created' if created else 'exists'} id={team.pk} slug={team.slug}")
    if not team.members.filter(pk=org_user.pk).exists():
        team.members.add(org_user)
        log("user added to team")

    # --- project (Project.save() of a new project creates its ProjectKey) ---
    project = Project.objects.filter(organization=org, name=project_name).first()
    if project is None:
        project = Project.objects.create(
            organization=org, name=project_name, platform=project_platform
        )
        log(f"project created id={project.pk} slug={project.slug}")
    else:
        log(f"project exists id={project.pk} slug={project.slug}")
    if not team.projects.filter(pk=project.pk).exists():
        team.projects.add(project)
        log("project linked to team")

    # --- project key: public key from env ---
    key = ProjectKey.objects.filter(project=project).order_by("pk").first()
    if key is None:
        key = ProjectKey.objects.create(project=project, public_key=public_key)
        log(f"project key created id={key.pk}")
    elif key.public_key != public_key:
        key.public_key = public_key
        key.save(update_fields=["public_key"])
        log(f"project key id={key.pk} public_key updated from env")

print(f"GLITCHTIP_BOOTSTRAP_OK project_id={project.pk} public_key={key.public_key.hex}")
