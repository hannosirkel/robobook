from __future__ import annotations  # noqa: I001

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import posting_policy  # noqa: E402


def posting_policy_fixture_with_profiles() -> dict:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "bank_accounts": {},
        "contacts": {"sales": {"woo": "42"}},
        "mappings": {
            "woo-taxable": {
                "income_account_id": "107",
                "shipping_income_account_id": "253",
                "vat_type_id": "25",
                "shipping_vat_type_id": "24",
                "warehouse_id": "9",
            }
        },
        "sales_vat_profiles": [
            {"start": "2025-01-01", "end": "2025-06-30", "rate": 22,
             "goods_vat_type_id": "25", "shipping_vat_type_id": "24"},
            {"start": "2025-07-01", "end": None, "rate": 24,
             "goods_vat_type_id": "34", "shipping_vat_type_id": "33"},
        ],
        "supplier_aliases": {},
    }


def statement_import_policy_fixture() -> dict:
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "bank_accounts": {"EE001234567890": {"EUR": "3", "USD": "3"}},
        "contacts": {},
        "mappings": {},
        "supplier_aliases": {},
        "cash_posting": {
            "mode": "statement_import",
            "bank_income_account_ids": ["3"],
            "processor_income_account_ids": {"paypal": "6", "stripe": "7"},
            "bank_financial_accounts": {"EE001234567890": {"EUR": "10", "USD": "11"}},
            "clearing_provider_roles": {"paypal": "paypal", "stripe": "stripe_clearing"},
            "financial_accounts": {
                "stripe_clearing": "30",
                "paypal": "31",
                "bank_fees": "32",
                "reporting_person_payable": "33",
                "platform_prepayment": "34",
                "fx_gain": "35",
                "fx_loss": "36",
                "customer_receivable": "37",
                "supplier_payable": "38",
                "bank": "10",
            },
        },
        "warehouse_routing": {
            "woo": {"before_order": 1000, "before_warehouse_id": "6", "from_warehouse_id": "1"},
            "direct_sale_warehouse_id": "1",
            "distributor_warehouse_id": "9",
        },
    }


class PostingPolicyTests(unittest.TestCase):
    def test_linked_invoice_receipt_uses_sales_contact_role(self) -> None:
        policy = {
            "bank_accounts": {"EE123": {"EUR": "3"}},
            "contacts": {"sales": {"brain-games": "31"}, "processors": {}, "suppliers": {}},
            "mappings": {},
        }
        action = {
            "action_type": "create_incoming_summary",
            "payload": {
                "draft_schema": "cash_settlement_v1",
                "document_type": "incoming",
                "linked_invoice_id": "58",
                "counterparty_hint": "brain-games",
                "counterparty": {"contact_id": "31"},
                "bank_account_id": "3",
            },
        }

        self.assertEqual(posting_policy.action_policy_errors(action, policy), [])

    def test_resolve_sales_vat_profile_changes_on_effective_date(self) -> None:
        policy = posting_policy_fixture_with_profiles()

        self.assertEqual(
            posting_policy.resolve_sales_vat_profile(policy, event_date=date(2025, 6, 30))["rate"], 22
        )
        self.assertEqual(
            posting_policy.resolve_sales_vat_profile(policy, event_date=date(2025, 7, 1))["rate"], 24
        )

    def test_policy_rejects_overlapping_sales_vat_profiles(self) -> None:
        policy = posting_policy_fixture_with_profiles()
        policy["sales_vat_profiles"][1]["start"] = "2025-06-30"

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "overlap"):
            posting_policy.validate_posting_policy(policy)

    def test_bank_account_resolution_requires_exact_source_account(self) -> None:
        policy = {"bank_accounts": {"EE-LHV": "3"}}

        self.assertEqual(posting_policy.resolve_bank_account(policy, customer_account="EE-LHV"), "3")
        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.resolve_bank_account(policy, customer_account="UNKNOWN")

    def test_bank_account_resolution_requires_exact_currency_mapping(self) -> None:
        policy = {"bank_accounts": {"EE-LHV": {"EUR": "3", "USD": "4"}}}

        self.assertEqual(
            posting_policy.resolve_bank_account(policy, customer_account="ee-lhv", currency="eur"),
            "3",
        )
        self.assertEqual(
            posting_policy.resolve_bank_account(policy, customer_account="EE-LHV", currency="USD"),
            "4",
        )
        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "GBP"):
            posting_policy.resolve_bank_account(policy, customer_account="EE-LHV", currency="GBP")

        legacy = {"bank_accounts": {"EE-LHV": "3"}}
        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "specify currency 'USD'"):
            posting_policy.resolve_bank_account(legacy, customer_account="EE-LHV", currency="USD")
        self.assertEqual(
            posting_policy.resolve_bank_account(
                legacy,
                customer_account="EE-LHV",
                currency="USD",
                allow_legacy_single_currency=True,
            ),
            "3",
        )

    def test_policy_rejects_non_uppercase_currency_bank_mapping(self) -> None:
        policy = posting_policy_fixture_with_profiles()
        policy["bank_accounts"] = {"EE-LHV": {"usd": "4"}}

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "invalid currency"):
            posting_policy.validate_posting_policy(policy)

    def test_woo_uses_eraisik_and_paypal_never_falls_back_to_stripe(self) -> None:
        policy = {
            "contacts": {
                "sales": {"woo": "42"},
                "processors": {"stripe": "29"},
            }
        }

        self.assertEqual(posting_policy.resolve_contact(policy, role="sales", label="woo"), "42")
        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.resolve_contact(policy, role="processors", label="paypal")

    def test_load_policy_rejects_missing_required_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "posting_policy.json"
            path.write_text('{"schema_version":"1.0"}', encoding="utf-8")

            with self.assertRaises(posting_policy.PostingPolicyError):
                posting_policy.load_posting_policy(path)

    def test_policy_rejects_non_numeric_submit_mapping_id(self) -> None:
        policy = {
            "schema_version": "1.0",
            "company_slug": "example",
            "bank_accounts": {},
            "contacts": {},
            "mappings": {"purchase-printful": {"warehouse_id": "LV"}},
            "supplier_aliases": {},
        }

        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.validate_posting_policy(policy)

    def test_supplier_alias_resolution_is_explicit(self) -> None:
        policy = {"supplier_aliases": {"omniva": "as-eesti-post"}}

        self.assertEqual(posting_policy.resolve_supplier_alias(policy, "Omniva"), "as-eesti-post")
        self.assertEqual(posting_policy.resolve_supplier_alias(policy, "Unknown Supplier"), "unknown-supplier")


class StatementImportPolicyTests(unittest.TestCase):
    def test_statement_import_policy_requires_bank_and_financial_accounts(self) -> None:
        policy = statement_import_policy_fixture()

        posting_policy.validate_posting_policy(policy)

        self.assertEqual(posting_policy.cash_posting_mode(policy), "statement_import")
        resolved = posting_policy.statement_import_policy(policy)
        self.assertEqual(resolved["bank_income_account_ids"], ["3"])
        self.assertEqual(resolved["processor_income_account_ids"], {"paypal": "6", "stripe": "7"})
        self.assertEqual(resolved["financial_accounts"]["bank_fees"], "32")

    def test_cash_posting_mode_defaults_to_api_when_section_is_absent(self) -> None:
        policy = statement_import_policy_fixture()
        del policy["cash_posting"]

        posting_policy.validate_posting_policy(policy)

        self.assertEqual(posting_policy.cash_posting_mode(policy), "api")

    def test_statement_import_policy_rejects_api_mode(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"] = {"mode": "api"}

        posting_policy.validate_posting_policy(policy)

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "statement_import"):
            posting_policy.statement_import_policy(policy)

    def test_unknown_cash_posting_mode_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"]["mode"] = "manual"

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "mode"):
            posting_policy.validate_posting_policy(policy)

    def test_missing_financial_account_role_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        del policy["cash_posting"]["financial_accounts"]["fx_loss"]

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "fx_loss"):
            posting_policy.validate_posting_policy(policy)

    def test_unknown_financial_account_role_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"]["financial_accounts"]["bank_fee"] = "99"

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "bank_fee"):
            posting_policy.validate_posting_policy(policy)

    def test_non_numeric_financial_account_id_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"]["financial_accounts"]["bank_fees"] = "5350-fees"

        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.validate_posting_policy(policy)

    def test_empty_bank_income_account_ids_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"]["bank_income_account_ids"] = []

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "bank_income_account_ids"):
            posting_policy.validate_posting_policy(policy)

    def test_bank_and_processor_income_accounts_must_be_disjoint(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"]["processor_income_account_ids"]["stripe"] = "3"

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "disjoint"):
            posting_policy.validate_posting_policy(policy)


class SalesWarehouseRoutingTests(unittest.TestCase):
    def test_woo_warehouse_boundary_is_inclusive(self) -> None:
        policy = statement_import_policy_fixture()

        self.assertEqual(posting_policy.resolve_sales_warehouse(policy, channel="woo", order_number=999), "6")
        self.assertEqual(posting_policy.resolve_sales_warehouse(policy, channel="woo", order_number=1000), "1")

    def test_woo_routing_requires_an_exact_order_number(self) -> None:
        policy = statement_import_policy_fixture()

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "order number"):
            posting_policy.resolve_sales_warehouse(policy, channel="woo", order_number=None)

    def test_direct_sale_uses_the_reviewed_warehouse(self) -> None:
        policy = statement_import_policy_fixture()

        self.assertEqual(
            posting_policy.resolve_sales_warehouse(policy, channel="direct-sale", order_number=None), "1"
        )

    def test_bound_distributor_warehouse_resolves(self) -> None:
        policy = statement_import_policy_fixture()

        self.assertEqual(
            posting_policy.resolve_sales_warehouse(policy, channel="distributor", order_number=None), "9"
        )

    def test_unbound_distributor_warehouse_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        policy["warehouse_routing"]["distributor_warehouse_id"] = None

        posting_policy.validate_posting_policy(policy)

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "distributor"):
            posting_policy.resolve_sales_warehouse(policy, channel="distributor", order_number=None)

    def test_unknown_sales_channel_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "channel"):
            posting_policy.resolve_sales_warehouse(policy, channel="amazon", order_number=1)

    def test_incomplete_woo_routing_rule_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()
        del policy["warehouse_routing"]["woo"]["before_warehouse_id"]

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "before_warehouse_id"):
            posting_policy.validate_posting_policy(policy)


class StatementImportAccountBindingTests(unittest.TestCase):
    def test_statement_import_requires_receivable_and_payable_roles(self) -> None:
        policy = statement_import_policy_fixture()
        del policy["cash_posting"]["financial_accounts"]["supplier_payable"]

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "supplier_payable"):
            posting_policy.validate_posting_policy(policy)

    def test_statement_import_requires_bank_financial_accounts(self) -> None:
        policy = statement_import_policy_fixture()
        del policy["cash_posting"]["bank_financial_accounts"]

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "bank_financial_accounts"):
            posting_policy.validate_posting_policy(policy)

    def test_bank_financial_account_resolves_per_currency(self) -> None:
        policy = statement_import_policy_fixture()

        self.assertEqual(
            posting_policy.resolve_bank_financial_account(policy, iban="EE00 1234 5678 90", currency="EUR"), "10"
        )
        self.assertEqual(
            posting_policy.resolve_bank_financial_account(policy, iban="EE001234567890", currency="USD"), "11"
        )

    def test_unmapped_bank_iban_has_no_financial_account(self) -> None:
        policy = statement_import_policy_fixture()

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "bank_financial_accounts"):
            posting_policy.resolve_bank_financial_account(policy, iban="EE999", currency="EUR")

    def test_unmapped_bank_currency_has_no_financial_account(self) -> None:
        policy = statement_import_policy_fixture()

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "GBP"):
            posting_policy.resolve_bank_financial_account(policy, iban="EE001234567890", currency="GBP")

    def test_clearing_provider_resolves_to_a_reviewed_role_and_account(self) -> None:
        policy = statement_import_policy_fixture()

        self.assertEqual(posting_policy.resolve_clearing_account(policy, provider="PayPal"), ("paypal", "31"))
        self.assertEqual(posting_policy.resolve_clearing_account(policy, provider="stripe"), ("stripe_clearing", "30"))

    def test_unreviewed_clearing_provider_is_rejected(self) -> None:
        policy = statement_import_policy_fixture()

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "clearing provider"):
            posting_policy.resolve_clearing_account(policy, provider="wise")

    def test_clearing_provider_role_must_name_a_bound_financial_account(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"]["clearing_provider_roles"]["wise"] = "inventory_change"

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "inventory_change"):
            posting_policy.validate_posting_policy(policy)

    def test_clearing_provider_role_must_be_a_known_role(self) -> None:
        policy = statement_import_policy_fixture()
        policy["cash_posting"]["clearing_provider_roles"]["wise"] = "wise_clearing"

        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "wise_clearing"):
            posting_policy.validate_posting_policy(policy)


class ProcessorCashAccountTests(unittest.TestCase):
    def action(self, bank_account_id: str) -> dict:
        return {
            "action_type": "create_incoming_summary",
            "payload": {
                "draft_schema": "cash_settlement_v1",
                "document_type": "incoming",
                "linked_invoice_id": "58",
                "counterparty_hint": "paypal",
                "counterparty": {"contact_id": "31"},
                "bank_account_id": bank_account_id,
            },
        }

    def policy(self) -> dict:
        return dict(
            statement_import_policy_fixture(),
            contacts={"sales": {"paypal": "31"}, "processors": {}, "suppliers": {}},
        )

    def test_a_reviewed_processor_account_is_an_explicit_cash_account(self) -> None:
        self.assertEqual(posting_policy.action_policy_errors(self.action("6"), self.policy()), [])

    def test_an_unreviewed_cash_account_is_still_rejected(self) -> None:
        errors = posting_policy.action_policy_errors(self.action("99"), self.policy())

        self.assertIn("not one of the explicit posting-policy accounts", " ".join(errors))


class DeclaredWarehouseRoutingTests(unittest.TestCase):
    def policy(self) -> dict:
        return dict(
            statement_import_policy_fixture(),
            contacts={"sales": {"woo": "42"}, "processors": {}, "suppliers": {}},
            mappings={
                "woo-taxable": {
                    "income_account_id": "107",
                    "vat_type_id": "25",
                    "warehouse_id": "6",
                    "article_id": "3",
                }
            },
        )

    def action(self, routing: dict | None, *, warehouse_hint: str) -> dict:
        summary_scope = {"channel_or_source": "woo", "posting_family": "woo-taxable", "tax_profile": "taxable"}
        if routing is not None:
            summary_scope["warehouse_routing"] = routing
        return {
            "action_type": "create_invoice_summary",
            "payload": {
                "document_date": "2024-01-31",
                "summary_scope": summary_scope,
                "posting_policy_family": "woo-taxable",
                "counterparty": {"contact_id": "42"},
                "totals": {"vat_amount": 10.0},
                "line_items": [{
                    "line_role": "sales_revenue",
                    "suggested_income_account_id": "107",
                    "suggested_vat_type_id": "25",
                    "warehouse_id_hint": warehouse_hint,
                    "article_id_hint": "3",
                }],
            },
        }

    def test_a_declared_routing_is_accepted_only_when_policy_reproduces_it(self) -> None:
        routing = {"channel": "woo", "warehouse_id": "1", "order_numbers": [1000, 1001]}

        self.assertEqual(
            posting_policy.action_policy_errors(self.action(routing, warehouse_hint="1"), self.policy()), []
        )

    def test_a_routing_the_policy_contradicts_is_rejected(self) -> None:
        routing = {"channel": "woo", "warehouse_id": "1", "order_numbers": [999]}

        errors = posting_policy.action_policy_errors(self.action(routing, warehouse_hint="1"), self.policy())

        self.assertIn("routes to warehouse '6'", " ".join(errors))

    def test_a_line_that_ignores_its_declared_routing_is_rejected(self) -> None:
        routing = {"channel": "woo", "warehouse_id": "1", "order_numbers": [1000]}

        errors = posting_policy.action_policy_errors(self.action(routing, warehouse_hint="6"), self.policy())

        self.assertIn("Line warehouse does not match", " ".join(errors))

    def test_a_routing_without_order_numbers_is_rejected(self) -> None:
        routing = {"channel": "woo", "warehouse_id": "1", "order_numbers": []}

        errors = posting_policy.action_policy_errors(self.action(routing, warehouse_hint="1"), self.policy())

        self.assertIn("order numbers", " ".join(errors))

    def test_an_unrouted_action_still_uses_the_family_mapping(self) -> None:
        self.assertEqual(
            posting_policy.action_policy_errors(self.action(None, warehouse_hint="6"), self.policy()), []
        )


class NonInventoryEventTypeTests(unittest.TestCase):
    def policy(self, value: object) -> dict:
        return dict(statement_import_policy_fixture(), non_inventory_event_types=value)

    def test_a_reviewed_list_is_accepted(self) -> None:
        policy = self.policy(["paypal_chargeback", "paypal_dispute_fee"])

        posting_policy.validate_posting_policy(policy)

        self.assertEqual(
            posting_policy.non_inventory_event_types(policy),
            frozenset({"paypal_chargeback", "paypal_dispute_fee"}),
        )

    def test_an_absent_list_means_every_event_may_bear_inventory(self) -> None:
        policy = statement_import_policy_fixture()

        self.assertEqual(posting_policy.non_inventory_event_types(policy), frozenset())

    def test_a_non_list_is_rejected(self) -> None:
        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "non_inventory_event_types"):
            posting_policy.validate_posting_policy(self.policy("paypal_chargeback"))

    def action(self, *, event_types: list[str] | None, article: str | None) -> dict:
        line = {
            "line_role": "refund_revenue",
            "suggested_income_account_id": "109",
            "suggested_vat_type_id": "12",
            "warehouse_id_hint": "6",
            "article_id_hint": article,
        }
        if event_types is not None:
            line["contributor_event_types"] = event_types
        return {
            "action_type": "create_credit_invoice_summary",
            "payload": {
                "document_date": "2024-05-31",
                "summary_scope": {"channel_or_source": "woo", "posting_family": "woo-non-taxable"},
                "posting_policy_family": "woo-non-taxable",
                "counterparty": {"contact_id": "42"},
                "totals": {"vat_amount": 0.0},
                "line_items": [line],
            },
        }

    def mapped_policy(self) -> dict:
        return dict(
            self.policy(["paypal_chargeback"]),
            contacts={"sales": {"woo": "42"}, "processors": {}, "suppliers": {}},
            mappings={"woo-non-taxable": {
                "income_account_id": "109", "vat_type_id": "12",
                "warehouse_id": "6", "article_id": "3",
            }},
        )

    def test_a_declared_cash_reversal_may_carry_no_article(self) -> None:
        errors = posting_policy.action_policy_errors(
            self.action(event_types=["paypal_chargeback"], article=None), self.mapped_policy()
        )

        self.assertEqual([e for e in errors if "article" in e], [])

    def test_a_declared_cash_reversal_must_not_carry_one_either(self) -> None:
        errors = posting_policy.action_policy_errors(
            self.action(event_types=["paypal_chargeback"], article="3"), self.mapped_policy()
        )

        self.assertTrue([e for e in errors if "article" in e])

    def test_an_ordinary_line_still_requires_the_family_article(self) -> None:
        errors = posting_policy.action_policy_errors(
            self.action(event_types=None, article=None), self.mapped_policy()
        )

        self.assertTrue([e for e in errors if "article" in e])

    def test_an_empty_entry_is_rejected(self) -> None:
        with self.assertRaisesRegex(posting_policy.PostingPolicyError, "non_inventory_event_types"):
            posting_policy.validate_posting_policy(self.policy(["paypal_chargeback", " "]))


if __name__ == "__main__":
    unittest.main()


class ProcessorFeePaymentContactTests(unittest.TestCase):
    """A processor fee payment is owed to the processor, not to a generic supplier.

    `create_purchase_summary` already routes a processor vendor to the processors role;
    the payment that settles it must resolve the same contact or no batch can pass.
    """

    def policy(self) -> dict:
        return {
            "schema_version": "1.0",
            "company_slug": "example",
            "bank_accounts": {},
            "contacts": {"sales": {}, "processors": {"paypal": "63"}, "suppliers": {}},
            "mappings": {},
            "supplier_aliases": {},
        }

    def payment(self) -> dict:
        return {
            "action_type": "create_payment_summary",
            "payload": {
                "draft_schema": "cash_settlement_v1",
                "settlement_family": "processor-held",
                "vendor_hint": "paypal",
                "counterparty_hint": "paypal",
                "counterparty": {"contact_id": "63"},
                "bank_account_id": "6",
            },
        }

    def test_a_processor_fee_payment_resolves_the_processor_contact(self) -> None:
        errors = posting_policy.action_policy_errors(self.payment(), self.policy())

        self.assertEqual([e for e in errors if "contact" in e.lower()], [])

    def test_a_mismatched_processor_contact_is_still_reported(self) -> None:
        action = self.payment()
        action["payload"]["counterparty"]["contact_id"] = "999"

        errors = posting_policy.action_policy_errors(action, self.policy())

        self.assertTrue(any("999" in e for e in errors))


def dated_purchase_pin_policy() -> dict:
    """A supplier whose declared VAT rate follows Estonia's statutory changes."""
    return {
        "schema_version": "1.0",
        "company_slug": "example",
        "bank_accounts": {},
        "contacts": {},
        "mappings": {
            "purchase-dpd-eesti-as": {
                "expense_account_id": "126",
                "vat_type_id": [
                    {"start": "1900-01-01", "end": "2023-12-31", "vat_type_id": "3"},
                    {"start": "2024-01-01", "end": "2025-06-30", "vat_type_id": "26"},
                    {"start": "2025-07-01", "end": None, "vat_type_id": "35"},
                ],
            },
            "purchase-barn2": {"expense_account_id": "126", "vat_type_id": "19"},
        },
        "supplier_aliases": {},
    }


class DatedPurchaseVatPinTest(unittest.TestCase):
    def test_a_dated_pin_resolves_the_band_covering_the_event(self) -> None:
        resolved = posting_policy.resolve_mapping(
            dated_purchase_pin_policy(),
            family="purchase-dpd-eesti-as",
            field_name="vat_type_id",
            event_date=date(2024, 4, 20),
        )

        self.assertEqual(resolved, "26")

    def test_a_dated_pin_follows_a_later_statutory_rate_change(self) -> None:
        resolved = posting_policy.resolve_mapping(
            dated_purchase_pin_policy(),
            family="purchase-dpd-eesti-as",
            field_name="vat_type_id",
            event_date=date(2025, 9, 12),
        )

        self.assertEqual(resolved, "35")

    def test_a_scalar_pin_still_resolves_without_an_event_date(self) -> None:
        resolved = posting_policy.resolve_mapping(
            dated_purchase_pin_policy(),
            family="purchase-barn2",
            field_name="vat_type_id",
        )

        self.assertEqual(resolved, "19")

    def test_a_dated_pin_without_an_event_date_is_rejected(self) -> None:
        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.resolve_mapping(
                dated_purchase_pin_policy(),
                family="purchase-dpd-eesti-as",
                field_name="vat_type_id",
            )

    def test_an_event_covered_by_no_band_is_rejected(self) -> None:
        policy = dated_purchase_pin_policy()
        policy["mappings"]["purchase-dpd-eesti-as"]["vat_type_id"] = [
            {"start": "2024-01-01", "end": "2024-12-31", "vat_type_id": "26"},
        ]

        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.resolve_mapping(
                policy,
                family="purchase-dpd-eesti-as",
                field_name="vat_type_id",
                event_date=date(2025, 9, 12),
            )

    def test_overlapping_bands_are_rejected(self) -> None:
        policy = dated_purchase_pin_policy()
        policy["mappings"]["purchase-dpd-eesti-as"]["vat_type_id"] = [
            {"start": "2024-01-01", "end": "2025-12-31", "vat_type_id": "26"},
            {"start": "2025-07-01", "end": None, "vat_type_id": "35"},
        ]

        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.resolve_mapping(
                policy,
                family="purchase-dpd-eesti-as",
                field_name="vat_type_id",
                event_date=date(2025, 9, 12),
            )

    def test_a_policy_carrying_a_dated_pin_validates(self) -> None:
        policy = posting_policy_fixture_with_profiles()
        policy["mappings"]["purchase-dpd-eesti-as"] = {
            "expense_account_id": "126",
            "vat_type_id": [
                {"start": "2024-01-01", "end": "2025-06-30", "vat_type_id": "26"},
                {"start": "2025-07-01", "end": None, "vat_type_id": "35"},
            ],
        }

        posting_policy.validate_posting_policy(policy)

    def test_a_policy_carrying_overlapping_dated_bands_is_rejected(self) -> None:
        policy = posting_policy_fixture_with_profiles()
        policy["mappings"]["purchase-dpd-eesti-as"] = {
            "expense_account_id": "126",
            "vat_type_id": [
                {"start": "2024-01-01", "end": "2025-12-31", "vat_type_id": "26"},
                {"start": "2025-07-01", "end": None, "vat_type_id": "35"},
            ],
        }

        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.validate_posting_policy(policy)


class DatedPurchaseVatGuardTests(unittest.TestCase):
    """A pinned purchase rate that a statutory change has superseded must be caught.

    Estonia moved to 22% on 2024-01-01 and 24% on 2025-07-01. A static pin keeps
    declaring the old rate on correct amounts, which no zero-rate check can see.
    """

    def policy(self) -> dict:
        return {
            "schema_version": "1.0",
            "company_slug": "example",
            "bank_accounts": {},
            "contacts": {"suppliers": {"dpd-eesti-as": "77"}},
            "mappings": {
                "purchase-dpd-eesti-as": {
                    "expense_account_id": "126",
                    "vat_type_id": [
                        {"start": "1900-01-01", "end": "2023-12-31", "vat_type_id": "3"},
                        {"start": "2024-01-01", "end": "2025-06-30", "vat_type_id": "26"},
                        {"start": "2025-07-01", "end": None, "vat_type_id": "35"},
                    ],
                }
            },
            "supplier_aliases": {},
        }

    def purchase(self, *, document_date: str, vat_type_id: str) -> dict:
        return {
            "action_type": "create_purchase_summary",
            "payload": {
                "vendor_hint": "dpd-eesti-as",
                "counterparty": {"contact_id": "77"},
                "posting_policy_family": "purchase-dpd-eesti-as",
                "document_date": document_date,
                "line_items": [
                    {
                        "line_role": "expense",
                        "description": "courier",
                        "posting_policy_line_key": "courier",
                        "suggested_expense_account_id": "126",
                        "suggested_vat_type_id": vat_type_id,
                    }
                ],
            },
        }

    def test_the_rate_in_force_on_the_document_date_is_accepted(self) -> None:
        errors = posting_policy.action_policy_errors(
            self.purchase(document_date="2024-04-20", vat_type_id="26"), self.policy()
        )

        self.assertEqual([e for e in errors if "VAT" in e], [])

    def test_a_superseded_rate_on_a_2024_document_is_flagged(self) -> None:
        errors = posting_policy.action_policy_errors(
            self.purchase(document_date="2024-04-20", vat_type_id="3"), self.policy()
        )

        self.assertTrue([e for e in errors if "VAT" in e], f"expected a VAT error, got {errors}")

    def test_the_later_rate_change_is_enforced_too(self) -> None:
        errors = posting_policy.action_policy_errors(
            self.purchase(document_date="2025-09-12", vat_type_id="26"), self.policy()
        )

        self.assertTrue([e for e in errors if "VAT" in e], f"expected a VAT error, got {errors}")


class AcceptedCheckerWarningTests(unittest.TestCase):
    """A reviewed, permanently-true warning must be declarable, not silently bypassed.

    The live runner refuses to write while any checker warning is unresolved. Some
    of this company's warnings are structural — no clearing account carries
    normalized opening/closing balances, so continuity is provable nowhere — and
    will never clear. Declaring them keeps the gate meaningful: a warning nobody
    reviewed still stops the run.
    """

    def _policy(self, accepted: object) -> dict:
        policy = posting_policy_fixture_with_profiles()
        policy["accepted_checker_warnings"] = accepted
        return policy

    def test_a_declared_list_is_accepted(self) -> None:
        posting_policy.validate_posting_policy(
            self._policy(["Recon still carries", "Action confidence is medium"])
        )

    def test_the_declaration_is_optional(self) -> None:
        policy = posting_policy_fixture_with_profiles()

        self.assertEqual(posting_policy.accepted_checker_warnings(policy), [])

    def test_declared_entries_are_returned_in_order(self) -> None:
        policy = self._policy(["Recon still carries", "Policy memo suggests"])

        self.assertEqual(
            posting_policy.accepted_checker_warnings(policy),
            ["Recon still carries", "Policy memo suggests"],
        )

    def test_an_empty_entry_is_rejected(self) -> None:
        """A blank pattern would match every warning and silently disable the gate."""
        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.validate_posting_policy(self._policy(["Recon still carries", "  "]))

    def test_a_non_list_is_rejected(self) -> None:
        with self.assertRaises(posting_policy.PostingPolicyError):
            posting_policy.validate_posting_policy(self._policy("Recon still carries"))
