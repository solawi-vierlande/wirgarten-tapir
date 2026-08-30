from unittest.mock import Mock

from tapir.accounts.services.keycloak_user_manager import KeycloakUserManager
from tapir.wirgarten.tests.test_utils import TapirUnitTest


class TestGetKeycloakIdByEmail(TapirUnitTest):
    def test_returns_id_when_user_exists(self):
        kc = Mock()
        kc.get_users.return_value = [{"id": "abc-123"}]

        result = KeycloakUserManager.get_keycloak_id_by_email(kc, "user@example.com")

        self.assertEqual("abc-123", result)
        kc.get_users.assert_called_once_with(
            {"email": "user@example.com", "exact": True}
        )

    def test_returns_none_when_no_user(self):
        kc = Mock()
        kc.get_users.return_value = []

        result = KeycloakUserManager.get_keycloak_id_by_email(kc, "nobody@example.com")

        self.assertIsNone(result)

    def test_imported_user_with_username_different_from_email(self):
        kc = Mock()
        kc.get_users.return_value = [
            {
                "id": "imported-uuid",
                "username": "10339",
                "email": "imported@example.com",
            }
        ]

        result = KeycloakUserManager.get_keycloak_id_by_email(
            kc, "imported@example.com"
        )

        self.assertEqual("imported-uuid", result)
