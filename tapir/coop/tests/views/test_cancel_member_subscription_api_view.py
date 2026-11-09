import datetime
import json
from unittest import mock

from django.urls import reverse
from rest_framework import status

from tapir.wirgarten.constants import WEEKLY
from tapir.wirgarten.models import Subscription, SubscriptionChangeLogEntry
from tapir.wirgarten.parameters import ParameterDefinitions
from tapir.wirgarten.tapirmail import configure_mail_module
from tapir.wirgarten.tests.factories import (
    GrowingPeriodFactory,
    MemberFactory,
    ProductFactory,
    ProductPriceFactory,
    SubscriptionFactory,
)
from tapir.wirgarten.tests.test_utils import TapirIntegrationTest


class TestCancelMemberSubscriptionApiView(TapirIntegrationTest):
    @classmethod
    def setUpTestData(cls):
        ParameterDefinitions().import_definitions(bulk_create=True)
        configure_mail_module()

        cls.product = ProductFactory.create(type__delivery_cycle=WEEKLY[0])
        ProductPriceFactory.create(product=cls.product, size=1)
        cls.period_2025 = GrowingPeriodFactory.create(
            start_date=datetime.date(2025, 3, 1),
            end_date=datetime.date(2026, 2, 28),
        )
        cls.period_2026 = GrowingPeriodFactory.create(
            start_date=datetime.date(2026, 3, 1),
            end_date=datetime.date(2027, 2, 28),
        )

    def setUp(self) -> None:
        super().setUp()
        self.admin = MemberFactory.create(is_superuser=True)
        self.client.force_login(self.admin)
        self.member = MemberFactory.create(email="cancel@test.test")
        self.subscription = SubscriptionFactory.create(
            member=self.member,
            period=self.period_2025,
            product=self.product,
            quantity=1,
            start_date=self.period_2025.start_date,
            end_date=self.period_2025.end_date,
        )
        self.other_period_subscription = SubscriptionFactory.create(
            member=self.member,
            period=self.period_2026,
            product=self.product,
            quantity=1,
            start_date=self.period_2026.start_date,
            end_date=self.period_2026.end_date,
        )

    def _post(self, end_date="2026-02-28", cancellation_ts="2026-02-28T00:00:00+01:00"):
        return self.client.post(
            reverse("coop:admin-cancel-member-subscription"),
            data=json.dumps(
                {
                    "member_id": self.member.id,
                    "growing_period_id": self.period_2025.id,
                    "product_id": self.product.id,
                    "end_date": end_date,
                    "cancellation_ts": cancellation_ts,
                }
            ),
            content_type="application/json",
        )

    def test_post_cancelsOnlyTargetPeriodWithoutMail(self):
        with mock.patch(
            "tapir_mail.triggers.transactional_trigger.TransactionalTrigger.fire_action"
        ) as fire_action:
            response = self._post()

        self.assertStatusCode(response, status.HTTP_200_OK)
        body = response.json()
        self.assertTrue(body["cancelled"])
        self.assertFalse(body["already_cancelled"])
        self.assertFalse(body["deleted"])
        self.assertEqual(str(self.subscription.id), body["subscription_id"])

        self.subscription.refresh_from_db()
        self.assertEqual(datetime.date(2026, 2, 28), self.subscription.end_date)
        self.assertIsNotNone(self.subscription.cancellation_ts)
        self.assertIsNotNone(self.subscription.cancellation_admin_confirmed)

        self.other_period_subscription.refresh_from_db()
        self.assertIsNone(self.other_period_subscription.cancellation_ts)
        self.assertEqual(
            self.period_2026.end_date, self.other_period_subscription.end_date
        )

        self.assertEqual(
            1,
            SubscriptionChangeLogEntry.objects.filter(
                change_type=(
                    SubscriptionChangeLogEntry.SubscriptionChangeLogEntryType.CANCELLED
                )
            ).count(),
        )
        fire_action.assert_not_called()

    def test_post_secondRun_isIdempotent(self):
        self.assertStatusCode(self._post(), status.HTTP_200_OK)
        self.subscription.refresh_from_db()
        first_cancellation_ts = self.subscription.cancellation_ts

        response = self._post()

        self.assertStatusCode(response, status.HTTP_200_OK)
        body = response.json()
        self.assertFalse(body["cancelled"])
        self.assertTrue(body["already_cancelled"])
        self.assertFalse(body["deleted"])
        self.subscription.refresh_from_db()
        self.assertEqual(first_cancellation_ts, self.subscription.cancellation_ts)

    def test_post_endDateBeforeStart_deletesSubscription(self):
        response = self._post(end_date="2024-01-01")

        self.assertStatusCode(response, status.HTTP_200_OK)
        body = response.json()
        self.assertTrue(body["deleted"])
        self.assertFalse(
            Subscription.objects.filter(id=self.subscription.id).exists()
        )
        self.assertTrue(
            Subscription.objects.filter(
                id=self.other_period_subscription.id
            ).exists()
        )

    def test_post_missingSubscription_returns400(self):
        other_product = ProductFactory.create(type__delivery_cycle=WEEKLY[0])
        ProductPriceFactory.create(product=other_product, size=1)

        response = self.client.post(
            reverse("coop:admin-cancel-member-subscription"),
            data=json.dumps(
                {
                    "member_id": self.member.id,
                    "growing_period_id": self.period_2025.id,
                    "product_id": other_product.id,
                    "end_date": "2026-02-28",
                    "cancellation_ts": "2026-02-28T00:00:00+01:00",
                }
            ),
            content_type="application/json",
        )

        self.assertStatusCode(response, status.HTTP_400_BAD_REQUEST)