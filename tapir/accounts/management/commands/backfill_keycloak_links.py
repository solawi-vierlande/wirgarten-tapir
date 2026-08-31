import json
import logging
import sys

from django.core.management.base import BaseCommand

from tapir.accounts.services.keycloak_user_manager import KeycloakUserManager
from tapir.wirgarten.models import Member

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Backfill Member.keycloak_id by looking up each member's email in Keycloak. "
        "Mirrors the logic used by the admin import endpoint "
        "(_link_keycloak_if_requested), but operates on existing members."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Process every Member. Default scope if no other filter is given.",
        )
        parser.add_argument(
            "--email",
            action="append",
            default=[],
            help="Restrict to a specific email. Repeatable.",
        )
        parser.add_argument(
            "--member-id",
            action="append",
            default=[],
            dest="member_ids",
            help="Restrict to a specific Tapir member id. Repeatable.",
        )
        parser.add_argument(
            "--only-missing",
            action="store_true",
            default=True,
            help="Skip members whose keycloak_id is already set (default: True).",
        )
        parser.add_argument(
            "--include-linked",
            action="store_true",
            help="Process members even when keycloak_id is already set.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Overwrite an existing keycloak_id if the Keycloak lookup returns a different value.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print planned changes without writing.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=200,
            help="Chunk size for the queryset iteration (default: 200).",
        )
        parser.add_argument(
            "--output-format",
            choices=["json", "text"],
            default="json",
            help="Output format: json-lines (default, parseable) or text (human).",
        )

    def handle(self, *args, **options):
        only_missing = options["only_missing"] and not options["include_linked"]
        overwrite = options["overwrite"]
        dry_run = options["dry_run"]
        output_format = options["output_format"]
        batch_size = options["batch_size"]

        emails = [e for e in (options.get("email") or []) if e]
        member_ids = [m for m in (options.get("member_ids") or []) if m]

        qs = Member.objects.all()
        if emails:
            qs = qs.filter(email__in=emails)
        if member_ids:
            qs = qs.filter(id__in=member_ids)

        if only_missing:
            qs = qs.filter(keycloak_id__isnull=True)

        qs = qs.order_by("id")

        summary = {
            "total": 0,
            "linked": 0,
            "already_linked": 0,
            "skipped_different_keycloak_id": 0,
            "no_keycloak_user": 0,
            "skipped_no_email": 0,
            "errors": 0,
        }

        status_keys = set(summary.keys()) - {"total"}

        cache: dict = {}
        try:
            keycloak_client = KeycloakUserManager.get_keycloak_client(cache=cache)
        except Exception:
            log.exception("backfill_keycloak_links: failed to build Keycloak client")
            self._emit(
                output_format,
                {"error": "failed_to_build_keycloak_client"},
                is_summary=True,
                summary=summary,
            )
            sys.exit(2)

        offset = 0
        while True:
            batch = list(qs[offset : offset + batch_size])
            if not batch:
                break
            for member in batch:
                summary["total"] += 1
                status, keycloak_id = self._process_member(
                    member, keycloak_client, overwrite, dry_run
                )
                if status in status_keys:
                    summary[status] += 1
                else:
                    summary["errors"] += 1
                record = {
                    "member_id": str(member.id),
                    "email": member.email,
                    "status": status,
                }
                if keycloak_id is not None:
                    record["keycloak_id"] = keycloak_id
                self._emit(output_format, record, is_summary=False, summary=summary)
            offset += batch_size

        self._emit(
            output_format,
            {"summary": summary},
            is_summary=True,
            summary=summary,
        )

    def _process_member(self, member, keycloak_client, overwrite, dry_run):
        email = member.email
        if not email:
            return ("skipped_no_email", None)

        try:
            keycloak_id = KeycloakUserManager.get_keycloak_id_by_email(
                keycloak_client, email
            )
        except Exception:
            log.exception(
                "backfill_keycloak_links: Keycloak lookup failed for %s",
                email,
            )
            return ("error", None)

        if not keycloak_id:
            return ("no_keycloak_user", None)

        if member.keycloak_id and member.keycloak_id != keycloak_id:
            if not overwrite:
                log.warning(
                    "backfill_keycloak_links: member %s already linked to %s, "
                    "Keycloak reports %s; pass --overwrite to replace.",
                    member.id,
                    member.keycloak_id,
                    keycloak_id,
                )
                return ("skipped_different_keycloak_id", keycloak_id)
        elif member.keycloak_id == keycloak_id:
            return ("already_linked", keycloak_id)

        if dry_run:
            return ("linked", keycloak_id)

        Member.objects.filter(id=member.id).update(keycloak_id=keycloak_id)
        member.keycloak_id = keycloak_id
        return ("linked", keycloak_id)

    def _emit(self, output_format, record, is_summary, summary):
        if output_format == "json":
            self.stdout.write(json.dumps(record, ensure_ascii=False))
        else:
            if is_summary and "summary" in record:
                self.stdout.write("--- summary ---")
                for k, v in record["summary"].items():
                    self.stdout.write(f"{k}: {v}")
                self.stdout.write("--- end ---")
                return
            parts = [f"{k}={record[k]}" for k in record]
            self.stdout.write(" ".join(parts))
