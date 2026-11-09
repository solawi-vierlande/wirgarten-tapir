import datetime

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from tapir.bakery.services.breaddelivery_service import BreadDeliveryService
from tapir.payments.services.mandate_reference_provider import (
    MandateReferenceProvider,
)
from tapir.log.util import freeze_for_log
from tapir.subscriptions.services.notice_period_manager import NoticePeriodManager
from tapir.utils.models import (
    SubscriptionImportedLogEntry,
    SubscriptionImportUpdatedLogEntry,
)
from tapir.wirgarten.models import (
    GrowingPeriod,
    Member,
    MemberPickupLocation,
    PickupLocation,
    Product,
    Subscription,
)
from tapir.wirgarten.utils import get_now


class EnsureMemberSubscriptionsService:
    """Create missing subscriptions and correct quantities for one period.

    Never cancels, deletes or modifies subscriptions from other periods.
    An existing subscription in the target period keeps its fields but its
    ``quantity`` is aligned with the imported cart. The operation is
    idempotent.
    """

    @classmethod
    @transaction.atomic
    def ensure(
        cls,
        member: Member,
        growing_period: GrowingPeriod,
        shopping_cart: dict[str, int],
        pickup_location_ids: list[str],
        pickup_location_changes: list[dict] | None,
        actor,
    ) -> tuple[list[str], list[str], list[str], bool]:
        now = get_now(cache={})
        mandate_ref = MandateReferenceProvider.get_or_create_mandate_reference(
            member, cache={}
        )
        created_subscription_ids: list[str] = []
        updated_existing_product_ids: list[str] = []
        skipped_existing_product_ids: list[str] = []

        for product_id, quantity in shopping_cart.items():
            if quantity <= 0:
                continue
            product = get_object_or_404(Product, id=product_id)
            existing = cls._get_existing_subscription(
                member=member, product=product, growing_period=growing_period
            )
            if existing is not None:
                if existing.quantity == quantity:
                    skipped_existing_product_ids.append(str(product_id))
                    continue

                existing_before = freeze_for_log(existing)
                existing.quantity = quantity
                existing.save(update_fields=["quantity"])
                SubscriptionImportUpdatedLogEntry().populate(
                    old_frozen=existing_before,
                    new_model=existing,
                    actor=actor,
                    user=member,
                ).save()
                updated_existing_product_ids.append(str(product_id))
                continue

            subscription = Subscription.objects.create(
                member=member,
                product=product,
                period=growing_period,
                quantity=quantity,
                start_date=growing_period.start_date,
                end_date=growing_period.end_date,
                cancellation_ts=None,
                mandate_ref=mandate_ref,
                consent_ts=now,
                withdrawal_consent_ts=now,
                trial_disabled=True,
                trial_end_date_override=None,
                notice_period_duration=NoticePeriodManager.get_notice_period_duration(
                    product_type=product.type,
                    growing_period=growing_period,
                    cache={},
                ),
                notice_period_unit=NoticePeriodManager.get_notice_period_unit(
                    product_type=product.type,
                    growing_period=growing_period,
                    cache={},
                ),
                auto_confirmed=now,
            )
            created_at = timezone.make_aware(
                datetime.datetime.combine(
                    growing_period.start_date, datetime.time.min
                )
            )
            Subscription.objects.filter(id=subscription.id).update(
                created_at=created_at
            )
            SubscriptionImportedLogEntry().populate(
                model=subscription, actor=actor, user=member
            ).save()
            created_subscription_ids.append(str(subscription.id))

        pickup_location_created = cls._ensure_pickup_location(
            member=member,
            growing_period=growing_period,
            pickup_location_ids=pickup_location_ids,
            pickup_location_changes=pickup_location_changes,
        )

        if created_subscription_ids or updated_existing_product_ids:
            BreadDeliveryService.ensure_bread_deliveries_for_member(
                member, cache={}
            )

        return (
            created_subscription_ids,
            updated_existing_product_ids,
            skipped_existing_product_ids,
            pickup_location_created,
        )

    @staticmethod
    def _get_existing_subscription(
        member: Member, product: Product, growing_period: GrowingPeriod
    ) -> Subscription | None:
        return (
            Subscription.objects.filter(
                member=member, product=product, period=growing_period
            ).first()
            or Subscription.objects.filter(
                member=member,
                product=product,
                start_date=growing_period.start_date,
            ).first()
        )

    @staticmethod
    def _ensure_pickup_location(
        member: Member,
        growing_period: GrowingPeriod,
        pickup_location_ids: list[str],
        pickup_location_changes: list[dict] | None,
    ) -> bool:
        if pickup_location_changes:
            changed = False
            for change in pickup_location_changes:
                pickup_location = get_object_or_404(
                    PickupLocation, id=change["pickup_location_id"]
                )
                _, created = MemberPickupLocation.objects.update_or_create(
                    member=member,
                    valid_from=change["valid_from"],
                    defaults={"pickup_location": pickup_location},
                )
                changed = changed or created
            return changed

        if not pickup_location_ids:
            return False

        pickup_location = get_object_or_404(
            PickupLocation, id=pickup_location_ids[0]
        )
        _, created = MemberPickupLocation.objects.update_or_create(
            member=member,
            valid_from=growing_period.start_date,
            defaults={"pickup_location": pickup_location},
        )
        return created
