import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from tapir.bakery.services.breaddelivery_service import BreadDeliveryService
from tapir.wirgarten.models import (
    GrowingPeriod,
    Member,
    Product,
    Subscription,
    SubscriptionChangeLogEntry,
)
from tapir.wirgarten.utils import get_now

logger = logging.getLogger(__name__)


class CancelMemberSubscriptionService:
    """Cancel one subscription for one member/product/growing period.

    Unlike the member-facing cancellation API, this admin path:

    - uses the caller-supplied ``end_date`` / ``cancellation_ts`` instead of
      the notice-period logic and "today"
    - does not fire the ``CONTRACT_CANCELLED`` mail trigger
    - does not create questionnaire cancellation-reason rows
    - never touches subscriptions in other growing periods

    The operation is idempotent: if the subscription is already cancelled,
    it is left unchanged.
    """

    @classmethod
    @transaction.atomic
    def cancel(
        cls,
        member: Member,
        growing_period: GrowingPeriod,
        product: Product,
        end_date,
        cancellation_ts,
        actor,
    ) -> tuple[Subscription | None, bool, bool, object | None]:
        subscriptions = Subscription.objects.filter(
            member=member, product=product, period=growing_period
        )
        subscription_count = subscriptions.count()
        if subscription_count == 0:
            raise ValidationError(
                "No subscription found for this member/product/growing period."
            )
        if subscription_count > 1:
            raise ValidationError(
                "Multiple subscriptions found for this member/product/growing "
                "period; resolve manually."
            )

        subscription = subscriptions.first()
        previous_end_date = subscription.end_date

        if subscription.cancellation_ts is not None:
            logger.info(
                "CancelMemberSubscription: subscription %s already cancelled "
                "on %s, leaving unchanged",
                subscription.id,
                subscription.cancellation_ts,
            )
            return subscription, False, False, previous_end_date

        if timezone.is_naive(cancellation_ts):
            cancellation_ts = timezone.make_aware(
                cancellation_ts, timezone.get_current_timezone()
            )

        if end_date < subscription.start_date:
            SubscriptionChangeLogEntry().populate_subscription_changed(
                actor=actor,
                user=member,
                change_type=(
                    SubscriptionChangeLogEntry.SubscriptionChangeLogEntryType.CANCELLED
                ),
                subscriptions=[subscription],
                admin_confirmed=None,
            ).save()
            subscription.delete()
            logger.info(
                "CancelMemberSubscription: deleted subscription %s because "
                "end_date %s is before start_date %s",
                subscription.id,
                end_date,
                subscription.start_date,
            )
            return None, True, True, previous_end_date

        subscription.cancellation_ts = cancellation_ts
        subscription.end_date = end_date
        subscription.cancellation_admin_confirmed = get_now(cache={})
        subscription.save()

        SubscriptionChangeLogEntry().populate_subscription_changed(
            actor=actor,
            user=member,
            change_type=(
                SubscriptionChangeLogEntry.SubscriptionChangeLogEntryType.CANCELLED
            ),
            subscriptions=[subscription],
            admin_confirmed=get_now(cache={}),
        ).save()

        BreadDeliveryService.ensure_bread_deliveries_for_member(member, cache={})

        logger.info(
            "CancelMemberSubscription: cancelled subscription %s "
            "(member=%s, product=%s, period=%s) effective %s",
            subscription.id,
            member.id,
            product.id,
            growing_period.id,
            end_date,
        )
        return subscription, True, False, previous_end_date