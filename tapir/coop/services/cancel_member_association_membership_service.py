import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from tapir.associations.models import (
    AssociationMembership,
    AssociationMembershipDeletedLogEntry,
    AssociationMembershipUpdatedLogEntry,
)
from tapir.log.util import freeze_for_log
from tapir.wirgarten.models import Member

logger = logging.getLogger(__name__)


class CancelMemberAssociationMembershipService:
    """Cancel the association membership(s) active at a given end date.

    Unlike the member-facing cancellation API, this admin path:

    - uses the caller-supplied historical ``end_date`` / ``cancellation_ts``
    - does not fire the membership end-date mail trigger
    - leaves memberships that already ended before ``end_date`` untouched
    - deletes memberships that start after ``end_date``

    The operation is idempotent: an already cancelled active membership is
    left unchanged.
    """

    @classmethod
    @transaction.atomic
    def cancel(
        cls,
        member: Member,
        end_date,
        cancellation_ts,
        actor,
    ) -> tuple[bool, bool, list[str], list[str]]:
        memberships = list(
            AssociationMembership.objects.filter(member=member).order_by(
                "start_date"
            )
        )
        if not memberships:
            raise ValidationError(
                "No association membership found for this member."
            )

        if timezone.is_naive(cancellation_ts):
            cancellation_ts = timezone.make_aware(
                cancellation_ts, timezone.get_current_timezone()
            )

        active_memberships = [
            membership
            for membership in memberships
            if membership.start_date <= end_date
            and (
                membership.end_date is None
                or membership.end_date >= end_date
            )
        ]
        future_memberships = [
            membership
            for membership in memberships
            if membership.start_date > end_date
        ]

        cancelled_ids: list[str] = []
        deleted_ids: list[str] = []
        already_cancelled = False

        for membership in active_memberships:
            if membership.cancellation_ts is not None:
                already_cancelled = True
                continue
            if (
                membership.end_date is not None
                and membership.end_date < end_date
            ):
                continue

            before_changes = freeze_for_log(membership)
            membership.end_date = end_date
            membership.cancellation_ts = cancellation_ts
            membership.save()
            AssociationMembershipUpdatedLogEntry().populate(
                old_frozen=before_changes,
                new_model=membership,
                actor=actor,
                user=member,
            ).save()
            cancelled_ids.append(str(membership.id))

        for membership in future_memberships:
            AssociationMembershipDeletedLogEntry().populate_membership(
                membership=membership, actor=actor
            ).save()
            deleted_ids.append(str(membership.id))
            membership.delete()

        cancelled = bool(cancelled_ids or deleted_ids)
        if cancelled:
            logger.info(
                "CancelMemberAssociationMembership: member=%s cancelled=%s "
                "deleted=%s end_date=%s",
                member.id,
                cancelled_ids,
                deleted_ids,
                end_date,
            )
        else:
            logger.info(
                "CancelMemberAssociationMembership: member=%s already "
                "cancelled or no active membership at %s",
                member.id,
                end_date,
            )

        return cancelled, already_cancelled, cancelled_ids, deleted_ids