from textwrap import dedent

import django_ratelimit
from django.contrib import messages
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.hashers import check_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render, redirect
from django.urls import resolve, reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.urls.exceptions import Resolver404
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit

from backend.decorators import not_authenticated
from backend.models import LoginLog, User, VerificationCodes, AuditLog
from backend.core.views.auth.verify import create_magic_link
from backend.core.types.htmx import HtmxAnyHttpRequest
from settings.helpers import send_email, ARE_EMAILS_ENABLED

from settings.settings import (
    SOCIAL_AUTH_GITHUB_ENABLED,
    SOCIAL_AUTH_GOOGLE_OAUTH2_ENABLED,
)


@require_GET
@not_authenticated
def login_initial_page(request: HttpRequest):
    redirect_url = request.GET.get("next")

    return render(
        request,
        "pages/auth/login_initial.html",
        {"github_enabled": SOCIAL_AUTH_GITHUB_ENABLED, "next": redirect_url, "google_enabled": SOCIAL_AUTH_GOOGLE_OAUTH2_ENABLED},
    )


@not_authenticated
@require_POST
def login_manual(request: HttpRequest):
    email = request.POST.get("email")
    password = request.POST.get("password")
    redirect_url = request.POST.get("next", "")

    if not email:
        messages.error(request, "Please enter an email")
        return redirect_to_login("", redirect_url)

    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Please enter a valid email")
        return redirect_to_login("", redirect_url)

    if not password:
        messages.error(request, "Please enter a password")
        return redirect_to_login(email, redirect_url)

    user = authenticate(request, username=email, password=password)

    if not user:
        messages.error(request, "Incorrect email or password")
        return redirect_to_login(email, redirect_url)

    if user.awaiting_email_verification and ARE_EMAILS_ENABLED:  # type: ignore[attr-defined]
        messages.error(request, "You must verify your email before logging in.")
        return redirect_to_login(email, redirect_url)

    login(request, user)

    if user.require_change_password:  # type: ignore[attr-defined]
        messages.warning(request, "You have been requested by an administrator to change your account password.")
        return redirect("settings:change_password")

    if url_has_allowed_host_and_scheme(redirect_url, allowed_hosts=None):
        try:
            resolve(redirect_url)
            return redirect(redirect_url)
        except Resolver404:
            return redirect("dashboard")
    else:
        return redirect("dashboard")


def redirect_to_login(email: str, redirect_url: str):
    if not url_has_allowed_host_and_scheme(redirect_url, allowed_hosts=None):
        redirect_url = reverse("dashboard")
    return redirect(f"{reverse('auth:login')}?email={email}&next={redirect_url}")


def render_error_toast_message(request: HttpRequest, message: str) -> HttpResponse:
    messages.error(request, message)
    return render_toast_message(request)


def render_toast_message(request: HttpRequest) -> HttpResponse:
    return render(request, "base/toasts.html")  # htmx will handle the toast


class MagicLinkRequestView(View):
    @method_decorator(ratelimit(key="post:email", method=django_ratelimit.UNSAFE, rate="5/m"))
    @method_decorator(ratelimit(key="post:email", method=django_ratelimit.UNSAFE, rate="10/5m"))
    @method_decorator(ratelimit(key="ip", method=django_ratelimit.UNSAFE, rate="2/m"))
    @method_decorator(ratelimit(key="ip", method=django_ratelimit.UNSAFE, rate="3/10m"))
    @method_decorator(ratelimit(key="ip", method=django_ratelimit.UNSAFE, rate="6/1h"))
    def post(self, request: HtmxAnyHttpRequest) -> HttpResponse | bool:
        if request.user.is_authenticated:
            return redirect("dashboard")
        if not request.htmx:
            return redirect("auth:login")

        email = request.POST.get("email")
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return self.send_message(request)

        if not user.is_active:
            return self.send_message(request, "This account is not currently active.", False)

        magic_link, plain_token = create_magic_link(user, service="login")
        self.send_magic_link_email(request, user, str(magic_link.uuid), plain_token)
        self.send_message(request, should_redirect=False)
        return render(request, "pages/auth/magic_link_waiting.html", {"email": request.POST.get("email")})

    def send_message(
        self, request: HttpRequest, message: str = "", success: bool = True, should_redirect: bool = True
    ) -> HttpResponse | bool:
        message = message or "If this is a valid email address, we have sent you an email! Keep this tab open!"
        if success:
            messages.success(request, message)
        else:
            messages.error(request, message)

        if should_redirect:
            return render_toast_message(request)
        else:
            return True

    def send_magic_link_email(self, request: HttpRequest, user: User, uuid: str, plain_token: str) -> None:
        magic_link_url = request.build_absolute_uri(reverse("auth:login magic_link verify", kwargs={"uuid": uuid, "token": plain_token}))

        send_email(
            destination=user.email,
            subject="Login Request",
            content=dedent(
                f"""
            Hi {user.first_name if user.first_name else "User"},

            A login request was made on your MyFinances account. If this was not you, please ignore
            this email.

            If you would like to login, please use the following link: \n {magic_link_url}
        """
            ),
        )


class MagicLinkWaitingView(View):
    def post(self, request: HtmxAnyHttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("dashboard")
        if not request.htmx:
            return redirect("auth:login")
        return render(request, "pages/auth/magic_link_waiting.html", {"email": request.POST.get("email")})


class MagicLinkVerifyView(View):
    def get(self, request: HttpRequest, uuid: str, token: str) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("dashboard")

        magic_link = get_magic_link(uuid)

        magic_link_valid, magic_link_msg = is_magiclink_valid(magic_link, token)
        if not magic_link_valid:
            messages.error(request, magic_link_msg)
            return redirect("auth:login")

        # user = magic_link.user
        # magic_link.delete()
        # user.backend = "backend.auth_backends.EmailInsteadOfUsernameBackend"
        # login(request, magic_link.user)

        return render(request, "pages/auth/magic_link_verify.html", {"uuid": uuid, "token": token})

        #
        # messages.success(request, "Successfully logged in")
        # # TODO: Add page to make sure they click an extra "verify request btn"
        # return redirect("dashboard")


class MagicLinkVerifyDecline(View):
    def post(self, request: HtmxAnyHttpRequest, uuid: str, token: str) -> HttpResponse:
        if request.user.is_authenticated or not request.htmx:
            return redirect("dashboard")

        magic_link = get_magic_link(uuid)
        magic_link_valid, magic_link_msg = is_magiclink_valid(magic_link, token)

        if not magic_link_valid or magic_link is None:
            messages.error(request, magic_link_msg)
            return render_toast_message(request)
        else:
            user = magic_link.user
            magic_link.delete()

            AuditLog.objects.create(user=user, action="magic link declined")
            messages.success(request, "Successfully declined the magic link verification request.")
            return render(request, "pages/auth/_magic_link_partial.html", {"declined": True})


class MagicLinkVerifyAccept(View):
    def post(self, request: HtmxAnyHttpRequest, uuid: str, token: str) -> HttpResponse:
        if request.user.is_authenticated or not request.htmx:
            return redirect("dashboard")

        magic_link = get_magic_link(uuid)
        magic_link_valid, magic_link_msg = is_magiclink_valid(magic_link, token)

        if not magic_link_valid or magic_link is None:
            messages.error(request, magic_link_msg)
            return render_toast_message(request)
        else:
            user = magic_link.user
            magic_link.delete()

            user.backend = "backend.auth_backends.EmailInsteadOfUsernameBackend"  # type: ignore[attr-defined]
            LoginLog.objects.create(user=user, service=LoginLog.ServiceTypes.MAGIC_LINK)
            AuditLog.objects.create(user=user, action="magic link accepted")
            login(request, magic_link.user)

            messages.success(request, "Successfully accepted the magic link verification request.")
            return render(request, "pages/auth/_magic_link_partial.html", {"accepted": True})


def is_magiclink_valid(magic_link: VerificationCodes | None, token: str) -> tuple[bool, str]:
    if not magic_link:
        return False, "Invalid magic link"

    if not magic_link.is_active():
        return False, "This link has expired"

    if not check_password(token, magic_link.token):
        return False, "Invalid magic link"

    return True, ""


def get_magic_link(uuid: str) -> VerificationCodes | None:
    try:
        return VerificationCodes.objects.get(uuid=uuid, service="login")
    except VerificationCodes.DoesNotExist:
        return None


def logout_view(request):
    logout(request)

    messages.success(request, "You've now been logged out.")

    return redirect("auth:login")  # + "?next=" + request.POST.get('next'))


@not_authenticated
def forgot_password_page(request: HttpRequest):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "pages/auth/forgot_password.html")
