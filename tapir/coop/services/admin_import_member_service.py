"""Admin-only direct DB import for a single Member.

This bypasses the BestellWizard fulfiller pipeline so we can import legacy /
already-validated data without hitting ``validate_phone_number_is_valid`` or
``validate_email_address_not_in_use``, and without triggering Keycloak sync,
mailing-list auto-subscribe, welcome mails, or the pickup-location
confirmation mail.

Everything happens inside ``transaction.atomic()`` so partial failures roll
back. Idempotent on email: re-POSTing the same payload returns the existing
Member rather than raising.

Coop (Genossenschaft) logic is intentionally absent — this Tapir instance
runs as a Verein (``LEGAL_STATUS_ASSOCIATION``). Callers can still attach an
``AssociationMembership`` via ``association_membership_type_id``.

Optional post-create timestamps (``date_joined``, ``sepa_consent``,
``withdrawal_consent``, ``privacy_consent``) are applied via a second
``.update()`` inside the same atomic block so callers can backdate the
record to its Budibase provenance. ``Member.created_at`` is
``auto_now_add=True`` and can only be overridden through this update path.
"""

from __future__ import annotations

import datetime
import logging

from django.db import transaction
from django.shortcuts import get_object_or_404

from tapir.accounts.services.keycloak_user_manager import KeycloakUserManager
from tapir.associations.models import AssociationMembershipType
from tapir.associations.services.association_membership_change_handler import (
    AssociationMembershipChangeHandler,
)
from tapir.payments.services.member_payment_rhythm_service import (
    MemberPaymentRhythmService,
)
from tapir.subscriptions.services.apply_tapir_order_manager import (
    ApplyTapirOrderManager,
)
from tapir.utils.models import MemberImportedLogEntry
from tapir.utils.services.tapir_cache import TapirCache
from tapir.wirgarten.models import (
    GrowingPeriod,
    Member,
    MemberPickupLocation,
    PickupLocation,
)

logger = logging.getLogger(__name__)


class AdminImportMemberService:
    """Perform a single Member import with subscriptions and pickup location."""

    @classmethod
    def import_member(
        cls,
        validated_data: dict,
        actor,
    ) -> tuple[Member, bool]:
        """Create (or return existing) Member + Subscriptions + PickingLocation.

        Returns ``(member, was_existing)``.
        """
        personal_data = validated_data["personal_data"]
        email = (personal_data.get("email") or "").strip().lower()

        # Resolve the caller-supplied Vereinsnummer. Top-level
        # `member_no` wins; then `personal_data.member_no`; finally
        # `personal_data.number` (legacy string-typed, coerced to int).
        # DRF has already coerced the top-level IntegerField + the
        # personal_data IntegerField; `personal_data.number` may be a
        # string.
        member_no = validated_data.get("member_no")
        if member_no is None:
            member_no = personal_data.get("member_no")
        if member_no is None and personal_data.get("number"):
            try:
                member_no = int(personal_data["number"])
            except (TypeError, ValueError):
                member_no = None

        existing = Member.objects.filter(email__iexact=email).first()
        if existing is not None:
            logger.info(
                "AdminImportMember: member with email %s already exists, returning",
                email,
            )
            return existing, True

        with transaction.atomic():
            member = cls._create_member(personal_data, member_no=member_no)

            cls._apply_payment_rhythm(
                member=member,
                rhythm=validated_data["payment_rhythm"],
                actor=actor,
            )

            growing_period = get_object_or_404(
                GrowingPeriod, id=validated_data["growing_period_id"]
            )
            contract_start_date = growing_period.start_date

            cls._create_subscriptions(
                member=member,
                shopping_cart=validated_data["shopping_cart_order"],
                growing_period=growing_period,
                contract_start_date=contract_start_date,
                actor=actor,
            )

            cls._create_pickup_location(
                member=member,
                pickup_location_ids=validated_data["pickup_location_ids"],
                valid_from=contract_start_date,
            )

            cls._create_association_membership(
                member=member,
                association_membership_type_id=validated_data.get(
                    "association_membership_type_id"
                ),
                start_date=validated_data.get("date_joined"),
                actor=actor,
            )

            cls._link_keycloak_if_requested(
                member=member,
                personal_data=personal_data,
                keycloak_lookup=validated_data.get("keycloak_lookup", True),
            )

            MemberImportedLogEntry().populate(
                model=member, actor=actor, user=member
            ).save()

            cls._apply_post_create_timestamps(
                member=member, validated_data=validated_data
            )

        member.refresh_from_db()
        return member, False

    @classmethod
    def _create_member(
        cls, personal_data: dict, member_no: int | None = None
    ) -> Member:
        now = datetime.datetime.now()
        contracts_signed = dict.fromkeys(
            ["sepa_consent", "withdrawal_consent", "privacy_consent"], now
        )

        member = Member(
            **personal_data,
            **contracts_signed,
            is_student=bool(personal_data.get("is_student", False)),
        )
        if member_no is not None:
            # Caller-supplied Vereinsnummer (Budibase `customernumber`).
            # Bypasses `MemberNumberService` so the value matches the
            # source-of-truth. `Member.member_no` is `unique=True, null=True`
            # so duplicate ints raise IntegrityError on save().
            member.member_no = member_no
        # bypass_keycloak=True skips the KeycloakUserManager.create_keycloak_user
        # path inside KeycloakUser.save().
        member.save(bypass_keycloak=True)
        return member

    @classmethod
    def _apply_payment_rhythm(cls, member: Member, rhythm: str, actor):
        # assign_payment_rhythm_to_member writes a PaymentTransaction + log
        # entries but does not send user-facing emails.
        MemberPaymentRhythmService.assign_payment_rhythm_to_member(
            member=member,
            rhythm=rhythm,
            valid_from=datetime.date.today(),
            cache={},
            actor=actor,
        )

    @classmethod
    def _create_subscriptions(
        cls,
        member: Member,
        shopping_cart: dict,
        growing_period: GrowingPeriod,
        contract_start_date: datetime.date,
        actor,
    ):
        from tapir.subscriptions.services.tapir_order_builder import (
            TapirOrderBuilder,
        )

        cache: dict = {}
        TapirCache.get_growing_period_at_date(
            reference_date=contract_start_date, cache=cache
        )

        order = TapirOrderBuilder.build_tapir_order_from_shopping_cart_serializer(
            shopping_cart, cache=cache
        )

        if not order:
            return

        # Reuses the existing fulfiller so notice period, mandate reference,
        # and product-capacity checks stay consistent with the rest of Tapir.
        # The OnboardingTrigger.on_subscription_updated call inside
        # ApplyTapirOrderManager only queues future-dated emails, which won't
        # fire immediately for legitimate season starts.
        ApplyTapirOrderManager.apply_order_with_several_product_types(
            member=member,
            order=order,
            contract_start_date=contract_start_date,
            actor=actor,
            needs_admin_confirmation=False,
            cache=cache,
        )

    @classmethod
    def _create_pickup_location(
        cls,
        member: Member,
        pickup_location_ids: list,
        valid_from: datetime.date,
    ):
        # Skip MemberPickupLocationSetter.link_member_to_pickup_location
        # because it fires TransactionalTrigger for a pickup-change email.
        # We write the row directly.
        for pickup_location_id in pickup_location_ids:
            pickup_location = get_object_or_404(PickupLocation, id=pickup_location_id)
            MemberPickupLocation.objects.create(
                member=member,
                pickup_location=pickup_location,
                valid_from=valid_from,
            )

    @classmethod
    def _create_association_membership(
        cls,
        member: Member,
        association_membership_type_id: str | None,
        start_date: datetime.datetime | None,
        actor,
    ):
        if not association_membership_type_id:
            logger.info(
                "AdminImportMember: no association_membership_type_id supplied for member %s, skipping Verein membership",
                member.email,
            )
            return

        membership_type = AssociationMembershipType.objects.get(
            id=association_membership_type_id
        )
        membership_start_date = (
            start_date.date() if isinstance(start_date, datetime.datetime) else None
        ) or datetime.date.today()

        AssociationMembershipChangeHandler.start_membership(
            member=member,
            association_membership_type=membership_type,
            start_date=membership_start_date,
            actor=actor,
            cache={},
        )

    @classmethod
    def _link_keycloak_if_requested(
        cls,
        member: Member,
        personal_data: dict,
        keycloak_lookup: bool,
    ):
        if not keycloak_lookup:
            return

        email = personal_data.get("email")
        if not email:
            return

        try:
            keycloak_client = KeycloakUserManager.get_keycloak_client(cache={})
            keycloak_id = KeycloakUserManager.get_keycloak_id_by_email(
                keycloak_client, email
            )
        except Exception:
            logger.exception(
                "AdminImportMember: Keycloak lookup failed for %s, leaving keycloak_id unset",
                email,
            )
            return

        if not keycloak_id:
            return

        # Use .update() to bypass KeycloakUser.save() (triggered via
        # Member.save() super) so we don't re-do the Keycloak sync we just
        # queried.
        Member.objects.filter(id=member.id).update(keycloak_id=keycloak_id)
        member.keycloak_id = keycloak_id

    @classmethod
    def _apply_post_create_timestamps(cls, member: Member, validated_data: dict):
        """Override auto_now_add/abstract-user timestamps when the caller
        supplies explicit values (typically backdating from Budibase
        provenance)."""
        timestamp_updates: dict[str, datetime.datetime] = {}

        date_joined = validated_data.get("date_joined")
        if date_joined is not None:
            timestamp_updates["date_joined"] = date_joined
            # Member.created_at is auto_now_add=True and cannot be set via save();
            # backdate it to the same source so the row's "first seen" matches.
            timestamp_updates["created_at"] = date_joined

        privacy_consent = validated_data.get("privacy_consent")
        if privacy_consent is not None:
            timestamp_updates["privacy_consent"] = privacy_consent

        if timestamp_updates:
            Member.objects.filter(id=member.id).update(**timestamp_updates)
