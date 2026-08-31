import datetime
import json
from unittest import mock

from django.urls import reverse
from rest_framework import status

from tapir.accounts.services.keycloak_user_manager import KeycloakUserManager
from tapir.associations.models import (
    AssociationMembership,
    AssociationMembershipType,
)
from tapir.associations.tests.factories import AssociationMembershipTypeFactory
from tapir.payments.models import MemberPaymentRhythm
from tapir.utils.models import MemberImportedLogEntry
from tapir.wirgarten.constants import WEEKLY
from tapir.wirgarten.models import (
    CoopShareTransaction,
    Member,
    MemberPickupLocation,
    Subscription,
)
from tapir.wirgarten.parameters import ParameterDefinitions
from tapir.wirgarten.tapirmail import configure_mail_module
from tapir.wirgarten.tests.factories import (
    PickupLocationFactory,
    GrowingPeriodFactory,
    MemberFactory,
    ProductFactory,
    ProductPriceFactory,
)
from tapir.wirgarten.tests.test_utils import TapirIntegrationTest, mock_timezone


def build_valid_payload(
    product,
    pickup_location,
    growing_period,
    *,
    email="new@imported.test",
    phone="017628244239",
    iban="NL37RABO2067756052",
    shopping_cart=None,
    pickup_location_ids=None,
    association_membership_type_id="",
    keycloak_lookup=True,
    date_joined=None,
    sepa_consent=None,
    withdrawal_consent=None,
    privacy_consent=None,
):
    payload = {
        "shopping_cart_order": shopping_cart or {product.id: 1},
        "shopping_cart_waiting_list": {},
        "personal_data": {
            "first_name": "John",
            "last_name": "Doe",
            "email": email,
            "phone_number": phone,
            "street": "Baker Street 221b",
            "street_2": "",
            "postcode": "16321",
            "city": "Berlin",
            "country": "DE",
            "account_owner": "John S. Doe",
            "iban": iban,
        },
        "sepa_allowed": True,
        "contract_accepted": True,
        "statute_accepted": True,
        "pickup_location_ids": pickup_location_ids or [pickup_location.id],
        "student_status_enabled": False,
        "payment_rhythm": MemberPaymentRhythm.Rhythm.SEMIANNUALLY,
        "become_member_now": True,
        "privacy_policy_read": True,
        "cancellation_policy_read": True,
        "growing_period_id": growing_period.id,
        "solidarity_contribution": 0.0,
        "distribution_channels": [],
        "feedback": "",
        "association_membership_type_id": association_membership_type_id,
        "keycloak_lookup": keycloak_lookup,
    }
    if date_joined is not None:
        payload["date_joined"] = date_joined.isoformat()
    if sepa_consent is not None:
        payload["sepa_consent"] = sepa_consent.isoformat()
    if withdrawal_consent is not None:
        payload["withdrawal_consent"] = withdrawal_consent.isoformat()
    if privacy_consent is not None:
        payload["privacy_consent"] = privacy_consent.isoformat()
    return payload


class TestAdminImportMemberApiView(TapirIntegrationTest):
    @classmethod
    def setUpTestData(cls):
        ParameterDefinitions().import_definitions(bulk_create=True)
        configure_mail_module()

        cls.product = ProductFactory.create(type__delivery_cycle=WEEKLY[0])
        cls.pickup_location = PickupLocationFactory.create()
        cls.growing_period = GrowingPeriodFactory.create(
            start_date=datetime.date(year=2027, month=1, day=1),
        )
        ProductPriceFactory.create(product=cls.product, size=1)

        cls.verein_type = AssociationMembershipTypeFactory.create(
            name="Vereinsmitglied"
        )
        cls.foerder_type = AssociationMembershipTypeFactory.create(
            name="Fördermitglied"
        )

    def setUp(self) -> None:
        super().setUp()
        mock_timezone(self, datetime.datetime(year=2027, month=6, day=27))

    def _mock_keycloak_lookup(self, keycloak_id):
        """Stub out KeycloakUserManager so tests don't require a Keycloak server."""
        patcher_client = mock.patch.object(
            KeycloakUserManager,
            "get_keycloak_client",
            classmethod(lambda cls, cache: object()),
        )
        patcher_lookup = mock.patch.object(
            KeycloakUserManager,
            "get_keycloak_id_by_email",
            classmethod(lambda cls, client, email: keycloak_id),
        )
        patcher_client.start()
        patcher_lookup.start()
        self.addCleanup(patcher_client.stop)
        self.addCleanup(patcher_lookup.stop)

    def test_post_happyPath_memberAndSubscriptionAndPickupLocationCreated(self):
        admin = MemberFactory.create(is_superuser=True)
        self.client.force_login(admin)
        self._mock_keycloak_lookup("kc-abc-123")

        payload = build_valid_payload(
            self.product,
            self.pickup_location,
            self.growing_period,
            association_membership_type_id=str(self.verein_type.id),
        )

        response = self.client.post(
            reverse("coop:admin-import-member"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertStatusCode(response, status.HTTP_200_OK)
        body = response.json()
        self.assertTrue(body["order_imported"])
        self.assertFalse(body["was_existing"])
        self.assertTrue(body["member_id"])

        self.assertEqual(1, Member.objects.count())
        member = Member.objects.get()
        self.assertEqual("new@imported.test", member.email)
        self.assertEqual("John", member.first_name)

        # Subscriptions
        self.assertEqual(1, Subscription.objects.count())
        sub = Subscription.objects.get()
        self.assertEqual(member, sub.member)
        self.assertEqual(self.product, sub.product)
        self.assertEqual(self.growing_period, sub.period)

        # PickupLocation
        self.assertEqual(1, MemberPickupLocation.objects.count())
        mpl = MemberPickupLocation.objects.get()
        self.assertEqual(member, mpl.member)
        self.assertEqual(self.pickup_location, mpl.pickup_location)

        # Coop shares: never created via this endpoint (Verein only)
        self.assertEqual(0, CoopShareTransaction.objects.count())

        # AssociationMembership for Verein
        self.assertEqual(1, AssociationMembership.objects.count())
        membership = AssociationMembership.objects.get()
        self.assertEqual(member, membership.member)
        self.assertEqual(self.verein_type, membership.type)
        self.assertEqual("Vereinsmitglied", membership.type.name)

        # Keycloak link applied
        self.assertEqual("kc-abc-123", member.keycloak_id)

        # Log entry written
        self.assertEqual(1, MemberImportedLogEntry.objects.count())

    def test_post_idempotent_rePostSamePayload_returnsExisting(self):
        admin = MemberFactory.create(is_superuser=True)
        self.client.force_login(admin)
        self._mock_keycloak_lookup("kc-abc-123")

        payload = build_valid_payload(
            self.product,
            self.pickup_location,
            self.growing_period,
            association_membership_type_id=str(self.verein_type.id),
        )

        response_1 = self.client.post(
            reverse("coop:admin-import-member"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertStatusCode(response_1, status.HTTP_200_OK)
        body_1 = response_1.json()
        self.assertFalse(body_1["was_existing"])
        member_id_1 = body_1["member_id"]

        # Re-POST same email, different case/whitespace -> still idempotent
        payload_2 = build_valid_payload(
            self.product,
            self.pickup_location,
            self.growing_period,
            email="  New@imported.test  ",
            association_membership_type_id=str(self.verein_type.id),
        )
        response_2 = self.client.post(
            reverse("coop:admin-import-member"),
            data=json.dumps(payload_2),
            content_type="application/json",
        )
        self.assertStatusCode(response_2, status.HTTP_200_OK)
        body_2 = response_2.json()
        self.assertTrue(body_2["was_existing"])
        self.assertEqual(member_id_1, body_2["member_id"])

        # No duplicate Member / Subscription / PickupLocation / Verein membership
        self.assertEqual(1, Member.objects.count())
        self.assertEqual(1, Subscription.objects.count())
        self.assertEqual(1, MemberPickupLocation.objects.count())
        self.assertEqual(0, CoopShareTransaction.objects.count())
        self.assertEqual(1, AssociationMembership.objects.count())

    def test_post_foerdermitglied_createsFördermitgliedMembership(self):
        admin = MemberFactory.create(is_superuser=True)
        self.client.force_login(admin)
        self._mock_keycloak_lookup("kc-abc-123")

        payload = build_valid_payload(
            self.product,
            self.pickup_location,
            self.growing_period,
            email="foerder@imported.test",
            association_membership_type_id=str(self.foerder_type.id),
        )

        response = self.client.post(
            reverse("coop:admin-import-member"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertStatusCode(response, status.HTTP_200_OK)
        self.assertEqual(0, CoopShareTransaction.objects.count())
        self.assertEqual(1, Member.objects.count())
        self.assertEqual(1, AssociationMembership.objects.count())
        membership = AssociationMembership.objects.get()
        self.assertEqual("Fördermitglied", membership.type.name)

    def test_post_keycloakLookupFalse_keycloakIdIsNull(self):
        admin = MemberFactory.create(is_superuser=True)
        self.client.force_login(admin)
        self._mock_keycloak_lookup("kc-should-not-be-used")

        payload = build_valid_payload(
            self.product,
            self.pickup_location,
            self.growing_period,
            keycloak_lookup=False,
        )

        response = self.client.post(
            reverse("coop:admin-import-member"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertStatusCode(response, status.HTTP_200_OK)
        self.assertEqual(1, Member.objects.count())
        self.assertIsNone(Member.objects.get().keycloak_id)

    def test_post_dateJoinedOverrides_createdAtEqualsDateJoined(self):
        admin = MemberFactory.create(is_superuser=True)
        self.client.force_login(admin)
        self._mock_keycloak_lookup("kc-abc-123")

        backdated = datetime.datetime(year=2026, month=2, day=15, hour=10, minute=30)
        payload = build_valid_payload(
            self.product,
            self.pickup_location,
            self.growing_period,
            date_joined=backdated,
            association_membership_type_id=str(self.verein_type.id),
        )

        response = self.client.post(
            reverse("coop:admin-import-member"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertStatusCode(response, status.HTTP_200_OK)
        member = Member.objects.get()
        self.assertEqual(backdated, member.date_joined)
        self.assertEqual(backdated, member.created_at)

    def test_post_missingAssociationMembershipType_stillImportsMember(self):
        admin = MemberFactory.create(is_superuser=True)
        self.client.force_login(admin)
        self._mock_keycloak_lookup("kc-abc-123")

        payload = build_valid_payload(
            self.product,
            self.pickup_location,
            self.growing_period,
            association_membership_type_id="",
        )

        response = self.client.post(
            reverse("coop:admin-import-member"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        # The endpoint does NOT 400 — the Member can still be created even
        # without a Verein membership (admin can add it later via Django admin).
        self.assertStatusCode(response, status.HTTP_200_OK)
        self.assertEqual(1, Member.objects.count())
        self.assertEqual(0, AssociationMembership.objects.count())

    def test_post_missingGrowingPeriod_returns400(self):
        admin = MemberFactory.create(is_superuser=True)
        self.client.force_login(admin)

        payload = build_valid_payload(
            self.product, self.pickup_location, self.growing_period
        )
        payload["growing_period_id"] = "00000000-0000-0000-0000-000000000000"

        response = self.client.post(
            reverse("coop:admin-import-member"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertStatusCode(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(0, Member.objects.count())

    def test_post_nonAdminUser_returns403(self):
        normal = MemberFactory.create(is_superuser=False)
        self.client.force_login(normal)

        payload = build_valid_payload(
            self.product, self.pickup_location, self.growing_period
        )

        response = self.client.post(
            reverse("coop:admin-import-member"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertStatusCode(response, status.HTTP_403_FORBIDDEN)
        self.assertEqual(0, Member.objects.count())

    def test_post_badPhoneNumber_succeedsBecauseValidatorsAreOff(self):
        """Phone validators are intentionally disabled for admin imports."""
        admin = MemberFactory.create(is_superuser=True)
        self.client.force_login(admin)
        self._mock_keycloak_lookup("kc-abc-123")

        payload = build_valid_payload(
            self.product,
            self.pickup_location,
            self.growing_period,
            phone="not-a-real-phone",
            email="badphone@imported.test",
        )

        response = self.client.post(
            reverse("coop:admin-import-member"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertStatusCode(response, status.HTTP_200_OK)
        self.assertEqual(1, Member.objects.count())
        self.assertEqual("not-a-real-phone", Member.objects.get().phone_number)
