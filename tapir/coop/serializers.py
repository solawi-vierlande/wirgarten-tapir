from rest_framework import serializers

from tapir.subscriptions.serializers import CoopShareTransactionSerializer


class MinimumNumberOfSharesResponseSerializer(serializers.Serializer):
    minimum_number_of_shares = serializers.IntegerField()


class GetCoopShareTransactionsResponseSerializer(serializers.Serializer):
    transactions = CoopShareTransactionSerializer(many=True)
    url_of_bestell_wizard = serializers.URLField()
    contact_email = serializers.EmailField()


class ExistingMemberPurchasesSharesRequestSerializer(serializers.Serializer):
    member_id = serializers.CharField()
    number_of_shares_to_add = serializers.IntegerField()
    iban = serializers.CharField(required=False, allow_blank=True)
    account_owner = serializers.CharField(required=False, allow_blank=True)
    as_admin = serializers.BooleanField()
    start_date = serializers.DateField(required=False)


class MemberBankDataResponseSerializer(serializers.Serializer):
    iban = serializers.CharField()
    account_owner = serializers.CharField()
    organisation_name = serializers.CharField()


class UpdateMemberBankDataRequestSerializer(serializers.Serializer):
    member_id = serializers.CharField()
    iban = serializers.CharField()
    account_owner = serializers.CharField()
    sepa_consent = serializers.BooleanField()


class MemberProfilePersonalDataResponseSerializer(serializers.Serializer):
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    phone_number = serializers.CharField()
    phone_number_landline = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    street = serializers.CharField()
    street_2 = serializers.CharField(allow_blank=True)
    postcode = serializers.CharField()
    city = serializers.CharField()
    is_student = serializers.BooleanField(required=False)
    can_edit_student = serializers.BooleanField()
    label_student_checkbox = serializers.CharField()
    can_edit_name = serializers.BooleanField()
    country = serializers.CharField(allow_blank=True)
    can_edit_country = serializers.BooleanField()
    contact_email = serializers.EmailField()
    member_number = serializers.CharField()
    phone_number_required = serializers.BooleanField()
    pseudonym = serializers.CharField(allow_blank=True)
    pseudonym_enabled = serializers.BooleanField()


class MemberProfilePersonalDataRequestSerializer(serializers.Serializer):
    member_id = serializers.CharField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    phone_number = serializers.CharField(allow_blank=True)
    phone_number_landline = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    street = serializers.CharField()
    street_2 = serializers.CharField(allow_blank=True)
    postcode = serializers.CharField()
    city = serializers.CharField()
    country = serializers.CharField(required=False)
    is_student = serializers.BooleanField(required=False)
    pseudonym = serializers.CharField(required=False, allow_blank=True, max_length=150)


class MemberEmailResponseSerializer(serializers.Serializer):
    email = serializers.EmailField()
    verified = serializers.BooleanField()
    contact_email = serializers.EmailField()
    is_admin = serializers.BooleanField()


class MemberEmailRequestSerializer(serializers.Serializer):
    member_id = serializers.CharField()
    email = serializers.EmailField()


class AdminImportMemberPersonalDataSerializer(serializers.Serializer):
    """Mirrors BestellWizardConfirmOrderRequestSerializer.PersonalDataSerializer
    but skips the phone/email validators that block imports for legacy data.

    email and phone_number are intentionally plain CharField (no EmailField,
    no phonenumbers check) so admins can fix bad data via Django admin later.
    """

    first_name = serializers.CharField()
    last_name = serializers.CharField()
    email = serializers.CharField()
    phone_number = serializers.CharField()
    street = serializers.CharField()
    street_2 = serializers.CharField(allow_blank=True)
    postcode = serializers.CharField()
    city = serializers.CharField()
    country = serializers.CharField()
    account_owner = serializers.CharField(allow_blank=True)
    iban = serializers.CharField(allow_blank=True)


class AdminImportMemberRequestSerializer(serializers.Serializer):
    """Admin-only DB import of a single Member with subscriptions and pickup
    location. Mirrors BestellWizardConfirmOrderRequestSerializer but with the
    PersonalData email/phone validators disabled and all coop (Genossenschaft)
    logic stripped — this Tapir instance runs as a Verein (LEGAL_STATUS_ASSOCIATION).
    """

    shopping_cart_order = serializers.DictField(child=serializers.IntegerField())
    shopping_cart_waiting_list = serializers.DictField(child=serializers.IntegerField())
    personal_data = AdminImportMemberPersonalDataSerializer()
    sepa_allowed = serializers.BooleanField()
    contract_accepted = serializers.BooleanField()
    statute_accepted = serializers.BooleanField()
    pickup_location_ids = serializers.ListField(child=serializers.CharField())
    student_status_enabled = serializers.BooleanField()
    payment_rhythm = serializers.CharField()
    become_member_now = serializers.BooleanField(allow_null=True)
    privacy_policy_read = serializers.BooleanField()
    cancellation_policy_read = serializers.BooleanField()
    growing_period_id = serializers.CharField()
    solidarity_contribution = serializers.FloatField()
    distribution_channels = serializers.ListField(child=serializers.CharField())
    feedback = serializers.CharField(allow_blank=True, required=False)
    association_membership_type_id = serializers.CharField(
        required=False, allow_blank=True
    )
    keycloak_lookup = serializers.BooleanField(required=False, default=True)
    date_joined = serializers.DateTimeField(required=False, allow_null=True)
    privacy_consent = serializers.DateTimeField(required=False, allow_null=True)
    # Optional: assign a specific `Member.member_no` (Vereinsnummer) on
    # import. If unset, the member is created without one and
    # `MemberNumberService` (or `backfill_member_numbers`) assigns one
    # later. Accepted at three payload locations for back-compat with
    # the legacy sync script: top-level `member_no`,
    # `personal_data.member_no`, `personal_data.number` (string coerced
    # to int). The first one that resolves wins (top-level > personal_data
    # > nested).
    member_no = serializers.IntegerField(required=False, allow_null=True)


class AdminImportMemberResponseSerializer(serializers.Serializer):
    member_id = serializers.CharField()
    was_existing = serializers.BooleanField()
    order_imported = serializers.BooleanField()
