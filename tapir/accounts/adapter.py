from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin, SocialAccount
from django.contrib.auth import get_user_model

from tapir.accounts.models import TapirUser
from tapir.accounts.services.keycloak_user_manager import KeycloakUserManager

usermodel = get_user_model()


class MySocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin: SocialLogin):
        tapir_user = TapirUser.objects.filter(email=sociallogin.user.email).first()
        if tapir_user is None:
            return

        if tapir_user.keycloak_id is None:
            keycloak_client = KeycloakUserManager.get_keycloak_client(cache={})
            # Look up by email: imported Keycloak users may have a username
            # (e.g. customernumber) that differs from their email.
            keycloak_id = KeycloakUserManager.get_keycloak_id_by_email(
                keycloak_client, tapir_user.email
            )
            tapir_user.keycloak_id = keycloak_id
            tapir_user.save()

        if SocialAccount.objects.filter(user=tapir_user).exists():
            return

        sociallogin.connect(request, tapir_user)

    def is_open_for_signup(self, request, sociallogin: SocialLogin) -> bool:
        return False
