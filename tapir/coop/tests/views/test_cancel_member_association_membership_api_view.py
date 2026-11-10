import datetime
import json
from unittest import mock

from django.urls import reverse
from rest_framework import status

from tapir.associations.models import (
    AssociationMembership,
    AssociationMembershipDeletedLogEntry,
    AssociationMembershipUpdatedLogEntry,
)
from tapir.associations.tests.factories import (
    AssociationMembershipFactory,
    AssociationMembershipTypeFactory,
)
from tapir.wirgarten.parameters import ParameterDefinitions
from tapir.wirgarten.tapirmail import configure_mail_module
from tapir.wirgarten.tests.factories import MemberFactory
from tapir.wirgarten.tests.test_utils import TapirIntegrationTest


class TestCancelMemberAssociationMembershipApiView(TapirIntegrationTest):
    @classmethod
    def setUpTestData(cls):
        ParameterDefinitions().import_definitions(bulk_create=True)
        configure_mail_module()

        cls.membership_type = AssociationMembershipTypeFactory.create(
            name="ordentliches Mitglied"
        )

    def setUp(self) -> None:
        super().setUp()
        self.admin = MemberFactory.create(is_superuser=True)
        self.client.force_login(self.admin)
        self.member = MemberFactory.create(email="membership-cancel@test.test")
        self.membership = AssociationMembershipFactory.create(
            member=self.member,
            type=self.membership_type,
            start_date=datetime.date(2021, 3, 1),
            end_date=None,
        )

    def _post(self, end_date="2022-02-28"):
        return self.client.post(
            reverse("coop:admin-cancel-member-association-membership"),
            data=json.dumps(
                {
                    "member_id": self.member.id,
                    "end_date": end_date,
                    "cancellation_ts": "2022-02-28T00:00:00+01:00",
                }
            ),
            content_type="application/json",
        )

    def test_post_cancelsActiveMembershipWithoutMail(self):
        with mock.patch(
            "tapir_mail.triggers.transactional_trigger.TransactionalTrigger.fire_action"
        ) as fire_action:
            response = self._post()

        self.assertStatusCode(response, status.HTTP_200_OK)
        body = response.json()
        self.assertTrue(body["cancelled"])
        self.assertFalse(body["already_cancelled"])
        self.assertEqual([str(self.membership.id)], body["cancelled_membership_ids"])
        self.assertEqual([], body["deleted_membership_ids"])

        self.membership.refresh_from_db()
        self.assertEqual(datetime.date(2022, 2, 28), self.membership.end_date)
        self.assertIsNotNone(self.membership.cancellation_ts)
        self.assertIsNone(self.membership.membership_ended_mail_sent_on)
        self.assertEqual(1, AssociationMembershipUpdatedLogEntry.objects.count())
        fire_action.assert_not_called()

    def test_post_secondRun_isIdempotent(self):
        self.assertStatusCode(self._post(), status.HTTP_200_OK)
        self.membership.refresh_from_db()
        first_ts = self.membership.cancellation_ts

        response = self._post()

        self.assertStatusCode(response, status.HTTP_200_OK)
        body = response.json()
        self.assertFalse(body["cancelled"])
        self.assertTrue(body["already_cancelled"])
        self.membership.refresh_from_db()
        self.assertEqual(first_ts, self.membership.cancellation_ts)
        self.assertEqual(1, AssociationMembershipUpdatedLogEntry.objects.count())

    def test_post_futureMembershipIsDeleted(self):
        future = AssociationMembershipFactory.create(
            member=self.member,
            type=self.membership_type,
            start_date=datetime.date(2023, 3, 1),
            end_date=None,
        )

        response = self._post()

        self.assertStatusCode(response, status.HTTP_200_OK)
        body = response.json()
        self.assertTrue(body["cancelled"])
        self.assertEqual([str(future.id)], body["deleted_membership_ids"])
        self.assertFalse(
            AssociationMembership.objects.filter(id=future.id).exists()
        )
        self.assertEqual(1, AssociationMembershipDeletedLogEntry.objects.count())

    def test_post_pastMembershipIsLeftUntouched(self):
        past = AssociationMembershipFactory.create(
            member=self.member,
            type=self.membership_type,
            start_date=datetime.date(2019, 3, 1),
            end_date=datetime.date(2020, 2, 29),
        )

        response = self._post()

        self.assertStatusCode(response, status.HTTP_200_OK)
        past.refresh_from_db()
        self.assertEqual(datetime.date(2020, 2, 29), past.end_date)
        self.assertIsNone(past.cancellation_ts)

    def test_post_noMembership_returns400(self):
        member_without_membership = MemberFactory.create()

        response = self.client.post(
            reverse("coop:admin-cancel-member-association-membership"),
            data=json.dumps(
                {
                    "member_id": member_without_membership.id,
                    "end_date": "2022-02-28",
                    "cancellation_ts": "2022-02-28T00:00:00+01:00",
                }
            ),
            content_type="application/json",
        )

        self.assertStatusCode(response, status.HTTP_400_BAD_REQUEST)