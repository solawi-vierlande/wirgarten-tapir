import io
import json
from unittest import mock

from django.core.management import call_command

from tapir.accounts.services.keycloak_user_manager import KeycloakUserManager
from tapir.wirgarten.models import Member
from tapir.wirgarten.parameters import ParameterDefinitions
from tapir.wirgarten.tests.factories import MemberFactory
from tapir.wirgarten.tests.test_utils import TapirIntegrationTest


class TestBackfillKeycloakLinksCommand(TapirIntegrationTest):
    @classmethod
    def setUpTestData(cls):
        ParameterDefinitions().import_definitions(bulk_create=True)

    @staticmethod
    def _mock_keycloak_lookup(keycloak_id_by_email):
        """Stub KeycloakUserManager so tests don't require a Keycloak server.

        `keycloak_id_by_email` is either a constant string (returned for every
        email) or a callable `email -> id | None`.
        """
        patcher_client = mock.patch.object(
            KeycloakUserManager,
            "get_keycloak_client",
            classmethod(lambda cls, cache: object()),
        )
        patcher_lookup = mock.patch.object(
            KeycloakUserManager,
            "get_keycloak_id_by_email",
            classmethod(
                lambda cls, client, email: (
                    keycloak_id_by_email(email)
                    if callable(keycloak_id_by_email)
                    else keycloak_id_by_email
                )
            ),
        )
        patcher_client.start()
        patcher_lookup.start()

        def cleanup():
            patcher_client.stop()
            patcher_lookup.stop()

        return cleanup

    @staticmethod
    def _parse_json_lines(output: str):
        records = []
        summary = None
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "summary" in obj:
                summary = obj["summary"]
            else:
                records.append(obj)
        return records, summary

    def test_emptyQueryset_noOutput_exitZero(self):
        cleanup = self._mock_keycloak_lookup("kc-1")
        self.addCleanup(cleanup)

        out = io.StringIO()
        call_command("backfill_keycloak_links", stdout=out)

        records, summary = self._parse_json_lines(out.getvalue())
        self.assertEqual([], records)
        self.assertIsNotNone(summary)
        self.assertEqual(0, summary["total"])

    def test_memberWithoutKeycloakId_lookupHits_writesKeycloakId(self):
        member = MemberFactory.create()
        Member.objects.filter(id=member.id).update(keycloak_id=None)
        member.refresh_from_db()

        cleanup = self._mock_keycloak_lookup(
            lambda email: "kc-resolved" if email == member.email else None
        )
        self.addCleanup(cleanup)

        out = io.StringIO()
        call_command("backfill_keycloak_links", stdout=out)

        records, summary = self._parse_json_lines(out.getvalue())
        self.assertEqual(1, summary["total"])
        self.assertEqual(1, summary["linked"])
        self.assertEqual(0, summary["already_linked"])
        self.assertEqual(1, len(records))
        self.assertEqual("linked", records[0]["status"])
        self.assertEqual("kc-resolved", records[0]["keycloak_id"])

        member.refresh_from_db()
        self.assertEqual("kc-resolved", member.keycloak_id)

    def test_memberAlreadyLinkedToSameId_alreadyLinked_noWrite(self):
        member = MemberFactory.create()
        Member.objects.filter(id=member.id).update(keycloak_id="kc-same")

        cleanup = self._mock_keycloak_lookup("kc-same")
        self.addCleanup(cleanup)

        out = io.StringIO()
        call_command("backfill_keycloak_links", "--include-linked", stdout=out)

        records, summary = self._parse_json_lines(out.getvalue())
        self.assertEqual(1, summary["total"])
        self.assertEqual(0, summary["linked"])
        self.assertEqual(1, summary["already_linked"])
        self.assertEqual(1, len(records))
        self.assertEqual("already_linked", records[0]["status"])
        self.assertEqual("kc-same", records[0]["keycloak_id"])

        member.refresh_from_db()
        self.assertEqual("kc-same", member.keycloak_id)

    def test_memberLinkedToDifferentId_skippedWithoutOverwrite(self):
        member = MemberFactory.create()
        Member.objects.filter(id=member.id).update(keycloak_id="kc-old")

        cleanup = self._mock_keycloak_lookup("kc-new")
        self.addCleanup(cleanup)

        out = io.StringIO()
        call_command("backfill_keycloak_links", "--include-linked", stdout=out)

        records, summary = self._parse_json_lines(out.getvalue())
        self.assertEqual(1, summary["total"])
        self.assertEqual(1, summary["skipped_different_keycloak_id"])
        self.assertEqual(0, summary["linked"])
        self.assertEqual("skipped_different_keycloak_id", records[0]["status"])
        self.assertEqual("kc-new", records[0]["keycloak_id"])

        member.refresh_from_db()
        self.assertEqual("kc-old", member.keycloak_id)

    def test_memberLinkedToDifferentId_overwriteReplaces(self):
        member = MemberFactory.create()
        Member.objects.filter(id=member.id).update(keycloak_id="kc-old")

        cleanup = self._mock_keycloak_lookup("kc-new")
        self.addCleanup(cleanup)

        out = io.StringIO()
        call_command(
            "backfill_keycloak_links", "--overwrite", "--include-linked", stdout=out
        )

        records, summary = self._parse_json_lines(out.getvalue())
        self.assertEqual(1, summary["linked"])
        self.assertEqual("linked", records[0]["status"])

        member.refresh_from_db()
        self.assertEqual("kc-new", member.keycloak_id)

    def test_memberLookupReturnsNone_marksNoKeycloakUser(self):
        member = MemberFactory.create()
        Member.objects.filter(id=member.id).update(keycloak_id=None)
        member.refresh_from_db()

        cleanup = self._mock_keycloak_lookup(
            lambda email: None if email == member.email else "kc-other"
        )
        self.addCleanup(cleanup)

        out = io.StringIO()
        call_command("backfill_keycloak_links", stdout=out)

        records, summary = self._parse_json_lines(out.getvalue())
        self.assertEqual(1, summary["total"])
        self.assertEqual(1, summary["no_keycloak_user"])
        self.assertEqual("no_keycloak_user", records[0]["status"])
        self.assertNotIn("keycloak_id", records[0])

        member.refresh_from_db()
        self.assertIsNone(member.keycloak_id)

    def test_keycloakClientRaises_marksError_continuesWithNext(self):
        m1 = MemberFactory.create()
        m2 = MemberFactory.create()
        Member.objects.filter(id__in=[m1.id, m2.id]).update(keycloak_id=None)
        m1.refresh_from_db()
        m2.refresh_from_db()

        patcher_client = mock.patch.object(
            KeycloakUserManager,
            "get_keycloak_client",
            classmethod(lambda cls, cache: object()),
        )

        def lookup(cls, client, email):
            if email == m1.email:
                raise RuntimeError("boom")
            return "kc-good"

        patcher_lookup = mock.patch.object(
            KeycloakUserManager,
            "get_keycloak_id_by_email",
            classmethod(lookup),
        )
        patcher_client.start()
        patcher_lookup.start()

        def cleanup():
            patcher_client.stop()
            patcher_lookup.stop()

        self.addCleanup(cleanup)

        out = io.StringIO()
        call_command("backfill_keycloak_links", stdout=out)

        records, summary = self._parse_json_lines(out.getvalue())
        self.assertEqual(2, summary["total"])
        self.assertEqual(1, summary["errors"])
        self.assertEqual(1, summary["linked"])

        statuses = sorted(r["status"] for r in records)
        self.assertEqual(["error", "linked"], statuses)
