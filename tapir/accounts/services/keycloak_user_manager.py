from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from keycloak import KeycloakOpenIDConnection, KeycloakAdmin
from keycloak.exceptions import KeycloakPostError

if TYPE_CHECKING:
    from tapir.accounts.models import KeycloakUser


class KeycloakUserManager:
    @classmethod
    def get_keycloak_id_by_email(
        cls, keycloak_client: KeycloakAdmin, email: str
    ) -> str | None:
        """Look up a Keycloak user by email address.

        Returns the Keycloak user ID (UUID), or None if no user has that email.
        Note: Keycloak users may have a `username` that differs from `email`
        (e.g. imported users with `username = customernumber`). Always look up
        by email, never by `keycloak_client.get_user_id(email)` which queries
        by `username`.
        """
        users = keycloak_client.get_users({"email": email, "exact": True})
        if not users:
            return None
        return users[0]["id"]

    @classmethod
    def create_keycloak_user_if_necessary(
        cls,
        user: KeycloakUser,
        initial_password: str | None,
    ):
        if user.keycloak_id:
            return

        data: dict = {
            "username": user.email,
            "email": user.email,
            "firstName": user.first_name,
            "lastName": user.last_name,
            "enabled": True,
        }

        keycloak_client = cls.get_keycloak_client()

        # Look up by email (not username) because imported KC users may have a
        # different username (e.g. customernumber).
        keycloak_id = cls.get_keycloak_id_by_email(keycloak_client, user.email)
        if keycloak_id is not None:
            user.keycloak_id = keycloak_id
            keycloak_client.update_user(user_id=user.keycloak_id, payload=data)
            SocialAccount.objects.create(
                user=user, provider="keycloak", uid=user.keycloak_id
            )
            user.save()
            return

        if initial_password:
            data["credentials"] = [{"value": initial_password, "type": "password"}]
            data["emailVerified"] = True
        else:
            data["requiredActions"] = ["VERIFY_EMAIL", "UPDATE_PASSWORD"]

        if user.is_superuser:
            data["groups"] = ["superuser"]
        else:
            data["groups"] = []

        try:
            user.keycloak_id = keycloak_client.create_user(data)
        except KeycloakPostError as e:
            # Race / duplicate: another process created a user with the same
            # email between our lookup and create. Re-fetch by email and reuse.
            if e.response_code == 409:
                keycloak_id = cls.get_keycloak_id_by_email(keycloak_client, user.email)
                if keycloak_id is not None:
                    user.keycloak_id = keycloak_id
                    keycloak_client.update_user(user_id=user.keycloak_id, payload=data)
                    SocialAccount.objects.create(
                        user=user, provider="keycloak", uid=user.keycloak_id
                    )
                    user.save()
                    return
            raise

        SocialAccount.objects.create(
            user=user, provider="keycloak", uid=user.keycloak_id
        )

        user.save()

    @classmethod
    def update_keycloak_user_name(cls, user: KeycloakUser):
        data = {"firstName": user.first_name, "lastName": user.last_name}
        keycloak_client = cls.get_keycloak_client()
        keycloak_client.update_user(user_id=user.keycloak_id, payload=data)

    @classmethod
    @functools.cache
    def get_keycloak_client(cls) -> KeycloakAdmin:
        config = settings.KEYCLOAK_ADMIN_CONFIG
        connection = KeycloakOpenIDConnection(
            server_url=config["SERVER_URL"],
            realm_name=config["REALM_NAME"],
            client_id=config["CLIENT_ID"],
            client_secret_key=config["CLIENT_SECRET_KEY"],
            verify=True,
        )
        return KeycloakAdmin(connection=connection)

    @classmethod
    def get_user_roles(cls, keycloak_id):
        keycloak_client = KeycloakUserManager.get_keycloak_client()

        raw_roles = keycloak_client.get_composite_realm_roles_of_user(
            keycloak_id,
        )

        return [
            raw_role["name"]
            for raw_role in raw_roles
            if raw_role["name"] not in settings.KEYCLOAK_NON_TAPIR_ROLES
        ]
