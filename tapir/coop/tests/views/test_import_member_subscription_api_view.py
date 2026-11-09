import datetime
import json

from django.urls import reverse
from rest_framework import status

from tapir.wirgarten.constants import WEEKLY
from tapir.wirgarten.models import MemberPickupLocation, Subscription
from tapir.wirgarten.parameters import ParameterDefinitions
from tapir.wirgarten.tapirmail import configure_mail_module
from tapir.wirgarten.tests.factories import (
    GrowingPeriodFactory,
    MemberFactory,
    PickupLocationFactory,
    ProductFactory,
    ProductPriceFactory,
    SubscriptionFactory,
)
from tapir.wirgarten.tests.test_utils import TapirIntegrationTest


class TestImportMemberSubscriptionApiView(TapirIntegrationTest):
    @classmethod
    def setUpTestData(cls):
        ParameterDefinitions().import_definitions(bulk_create=True)
        configure_mail_module()

        cls.product = ProductFactory.create(type__delivery_cycle=WEEKLY[0])
        ProductPriceFactory.create(product=cls.product, size=1)
        cls.pickup_location = PickupLocationFactory.create()
        cls.period_2025 = GrowingPeriodFactory.create(
            start_date=datetime.date(2025, 3, 1),
            end_date=datetime.date(2026, 2, 28),
        )
        cls.period_2026 = GrowingPeriodFactory.create(
            start_date=datetime.date(2026, 3, 1),
            end_date=datetime.date(2027, 2, 28),
        )
        cls.member = MemberFactory.create(email="existing@test.test")
        cls.subscription_2026 = SubscriptionFactory.create(
            member=cls.member,
            period=cls.period_2026,
            product=cls.product,
            quantity=1,
            start_date=cls.period_2026.start_date,
            end_date=cls.period_2026.end_date,
        )

    def setUp(self) -> None:
        super().setUp()
        admin = MemberFactory.create(is_superuser=True)
        self.client.force_login(admin)

    def _post(self, payload):
        return self.client.post(
            reverse("coop:admin-import-member-subscription"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _payload_2025(self, quantity=2):
        return {
            "member_id": self.member.id,
            "growing_period_id": self.period_2025.id,
            "shopping_cart_order": {self.product.id: quantity},
            "pickup_location_ids": [self.pickup_location.id],
        }

    def test_post_createsMissingSubscriptionWithoutTouchingOtherPeriods(self):
        response = self._post(self._payload_2025(quantity=2))

        self.assertStatusCode(response, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(1, len(body["created_subscription_ids"]))
        self.assertEqual([], body["updated_existing_product_ids"])
        self.assertEqual([], body["skipped_existing_product_ids"])
        self.assertTrue(body["pickup_location_created"])

        created = Subscription.objects.get(id=body["created_subscription_ids"][0])
        self.assertEqual(self.member, created.member)
        self.assertEqual(self.product, created.product)
        self.assertEqual(self.period_2025, created.period)
        self.assertEqual(2, created.quantity)
        self.assertEqual(self.period_2025.start_date, created.start_date)
        self.assertEqual(self.period_2025.end_date, created.end_date)

        self.subscription_2026.refresh_from_db()
        self.assertEqual(self.period_2026, self.subscription_2026.period)
        self.assertEqual(1, self.subscription_2026.quantity)
        self.assertEqual(
            self.period_2026.start_date, self.subscription_2026.start_date
        )
        self.assertEqual(self.period_2026.end_date, self.subscription_2026.end_date)

        pickup_location = MemberPickupLocation.objects.get(
            member=self.member, valid_from=self.period_2025.start_date
        )
        self.assertEqual(self.pickup_location, pickup_location.pickup_location)

    def test_post_secondRun_isIdempotent(self):
        self.assertStatusCode(self._post(self._payload_2025()), status.HTTP_200_OK)

        response = self._post(self._payload_2025())

        self.assertStatusCode(response, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual([], body["created_subscription_ids"])
        self.assertEqual([], body["updated_existing_product_ids"])
        self.assertEqual(
            [str(self.product.id)], body["skipped_existing_product_ids"]
        )
        self.assertEqual(
            1,
            Subscription.objects.filter(
                member=self.member, period=self.period_2025
            ).count(),
        )


    def test_post_existingSubscriptionInTargetPeriod_updatesQuantity(self):
        existing = SubscriptionFactory.create(
            member=self.member,
            period=self.period_2025,
            product=self.product,
            quantity=3,
            start_date=self.period_2025.start_date,
            end_date=self.period_2025.end_date,
        )

        response = self._post(self._payload_2025(quantity=2))

        self.assertStatusCode(response, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual([], body["created_subscription_ids"])
        self.assertEqual(
            [str(self.product.id)], body["updated_existing_product_ids"]
        )
        self.assertEqual([], body["skipped_existing_product_ids"])
        existing.refresh_from_db()
        self.assertEqual(2, existing.quantity)

    def test_post_pickupLocationChanges_createsTimeline(self):
        second_depot = PickupLocationFactory.create()
        payload = self._payload_2025()
        payload["pickup_location_changes"] = [
            {
                "valid_from": "2025-03-01",
                "pickup_location_id": self.pickup_location.id,
            },
            {
                "valid_from": "2025-09-01",
                "pickup_location_id": second_depot.id,
            },
        ]

        response = self._post(payload)

        self.assertStatusCode(response, status.HTTP_200_OK)
        self.assertTrue(response.json()["pickup_location_created"])
        rows = list(
            MemberPickupLocation.objects.filter(
                member=self.member
            ).order_by("valid_from")
        )
        self.assertEqual(2, len(rows))
        self.assertEqual(self.pickup_location, rows[0].pickup_location)
        self.assertEqual(second_depot, rows[1].pickup_location)
