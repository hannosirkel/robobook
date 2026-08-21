from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bookprep  # noqa: E402


def pdf_source(root: Path, name: str, *, source_system: str) -> bookprep.SourceDescriptor:
    path = root / name
    path.write_bytes(b"%PDF-1.4\n")
    return bookprep.SourceDescriptor(
        path=path,
        rel_path=str(path.relative_to(root)),
        source_id=bookprep.source_id_for_path(path, root_dir=root),
        source_type="pdf",
        source_system=source_system,
        covered_from=date(2023, 1, 1),
        covered_until=date(2023, 12, 31),
        canonical_group=bookprep.canonical_group_for_path(path),
        parser_name="parse_purchase_invoice_pdf",
        canonical=True,
    )


class BookprepTests(unittest.TestCase):
    def test_parser_accepts_woo_tax_allocation_override(self) -> None:
        args = bookprep.build_parser().parse_args(
            [
                "--company-dir", "companies/example", "--period", "2025-11",
                "--woo-tax-allocation", "reviewed-allocation.json",
            ]
        )

        self.assertEqual(args.woo_tax_allocation, "reviewed-allocation.json")

    def parse_tax_fixture(self, row: str, *, period: str = "2025-12"):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name) / "2025-pack"
        root.mkdir()
        path = root / "woocommerce-taxes.csv"
        path.write_text(
            "Tax code,Rate,Total tax,Order tax,Shipping tax,Orders\n" + row + "\n",
            encoding="utf-8",
        )
        period_start, period_end = bookprep.parse_period(period)
        source = bookprep.inspect_source_file(
            path=path, root_dir=root, period_start=period_start, period_end=period_end
        )
        assert source is not None
        return bookprep.parse_woo_tax_summary_csv(
            source, period_start=period_start, period_end=period_end, base_currency="EUR"
        )

    def test_parse_woo_tax_summary_csv_as_annual_supporting_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "2025-pack"
            root.mkdir()
            path = root / "woocommerce-taxes.csv"
            path.write_text(
                '\ufeff"Tax code",Rate,"Total tax","Order tax","Shipping tax",Orders\n'
                "DE-DE-VAT-1,19,19.00,15.00,4.00,1\n",
                encoding="utf-8",
            )
            start, end = bookprep.parse_period("2025-12")
            source = bookprep.inspect_source_file(path=path, root_dir=root, period_start=start, period_end=end)
            assert source is not None
            self.assertEqual(source.source_system, "woo")
            self.assertEqual(source.parser_name, "parse_woo_tax_summary_csv")
            self.assertEqual(source.covered_from.isoformat(), "2025-01-01")
            self.assertEqual(source.covered_until.isoformat(), "2025-12-31")
            records, exceptions = bookprep.parse_woo_tax_summary_csv(
                source, period_start=start, period_end=end, base_currency="EUR"
            )
            self.assertFalse(exceptions)
            self.assertEqual(records["sales"], [])
            self.assertEqual(records["other"][0]["event_type"], "woo_tax_summary")
            self.assertEqual(records["other"][0]["country_code"], "DE")
            self.assertEqual(records["other"][0]["attributes"]["orders"], 1)

    def test_woo_tax_summary_blocks_component_mismatch(self) -> None:
        records, exceptions = self.parse_tax_fixture("DE-DE-VAT-1,19,19.01,15.00,4.00,1")
        self.assertEqual(records["other"], [])
        self.assertTrue(any(item["blocking"] for item in exceptions))

    def test_woo_tax_summary_blocks_fractional_cent_tax_amounts(self) -> None:
        records, exceptions = self.parse_tax_fixture("DE-DE-VAT-1,19,19.005,15.004,4.001,1")
        self.assertEqual(records["other"], [])
        self.assertTrue(any(item["blocking"] for item in exceptions))

    def test_woo_tax_summary_emits_only_in_year_end_period(self) -> None:
        records, exceptions = self.parse_tax_fixture(
            "DE-DE-VAT-1,19,19.00,15.00,4.00,1", period="2025-05"
        )
        self.assertFalse(exceptions)
        self.assertEqual(records["other"], [])

    def test_woo_tax_summary_blocks_invalid_rate_code_and_count(self) -> None:
        for row in (
            "DE-DE-VAT-1,-1,19.00,15.00,4.00,1",
            "DE-DE-VAT-1,19,-1.00,0.00,-1.00,1",
            "DE-DE-VAT-1,19,19.00,15.00,4.00,1.5",
            "Germany-DE-VAT-1,19,19.00,15.00,4.00,1",
        ):
            with self.subTest(row=row):
                records, exceptions = self.parse_tax_fixture(row)
                self.assertEqual(records["other"], [])
                self.assertTrue(any(item["blocking"] for item in exceptions))

    def test_woo_tax_summary_blocks_missing_annual_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "woocommerce-taxes.csv"
            path.write_text(
                "Tax code,Rate,Total tax,Order tax,Shipping tax,Orders\n"
                "DE-DE-VAT-1,19,19.00,15.00,4.00,1\n",
                encoding="utf-8",
            )
            start, end = bookprep.parse_period("2025-12")
            source = bookprep.inspect_source_file(path=path, root_dir=root, period_start=start, period_end=end)
            assert source is not None
            self.assertEqual(source.covered_from, start)
            self.assertEqual(source.covered_until, end)
            _, exceptions = bookprep.parse_woo_tax_summary_csv(
                source, period_start=start, period_end=end, base_currency="EUR"
            )
            self.assertTrue(any(item["blocking"] for item in exceptions))

    def test_choose_canonical_sources_prefers_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("report_2023.csv", "report_2023.pdf", "report_2023.csv.gsheet"):
                (root / name).write_text("x", encoding="utf-8")

            period_start, period_end = bookprep.parse_period("2023-01")
            sources = [
                bookprep.inspect_source_file(path=root / name, root_dir=root, period_start=period_start, period_end=period_end)
                for name in ("report_2023.csv", "report_2023.pdf", "report_2023.csv.gsheet")
            ]
            selected = bookprep.choose_canonical_sources([source for source in sources if source is not None])

            canonical = [source for source in selected if source.canonical]
            self.assertEqual(len(canonical), 1)
            self.assertEqual(canonical[0].source_type, "csv")
            self.assertIn("report-2023-pdf", canonical[0].preferred_over)
            self.assertNotIn("report-2023-csv-gsheet", canonical[0].preferred_over)

    def test_choose_canonical_sources_treats_misnamed_xls_as_csv_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "orderHistory_2025.csv"
            xls_path = root / "orderHistory_2025.xls"
            csv_path.write_text(
                "ReferenceID,QMLOrderID,Email,Name,Status,OrderType,Carrier,ShippingType,TrackingNumber,DateSubmitted,DateShipped\n",
                encoding="utf-8",
            )
            xls_path.write_bytes(b"PK\x03\x04")

            period_start, period_end = bookprep.parse_period("2025-01")
            sources = [
                bookprep.inspect_source_file(path=path, root_dir=root, period_start=period_start, period_end=period_end)
                for path in (csv_path, xls_path)
            ]
            selected = bookprep.choose_canonical_sources([source for source in sources if source is not None])

            canonical = [source for source in selected if source.canonical]
            self.assertEqual(len(canonical), 1)
            self.assertEqual(canonical[0].path, csv_path)
            self.assertIn(bookprep.source_id_for_path(xls_path, root_dir=root), canonical[0].preferred_over)

    def test_gsheet_work_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gsheet_path = root / "report_2023.gsheet"
            gsheet_path.write_text('{"doc_id":"123"}', encoding="utf-8")

            period_start, period_end = bookprep.parse_period("2023-01")
            source = bookprep.inspect_source_file(path=gsheet_path, root_dir=root, period_start=period_start, period_end=period_end)
            self.assertIsNone(source)

    def test_source_ids_include_relative_path_to_avoid_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "b").mkdir()
            for path in (root / "a" / "transactions.csv", root / "b" / "transactions.csv"):
                path.write_text(
                    "Date,Orders,Gross sales,Returns,Coupons,Net sales,Taxes,Shipping,Total sales\n"
                    "2023-01-15,1,10.00,0.00,0.00,10.00,2.00,0.00,12.00\n",
                    encoding="utf-8",
                )

            period_start, period_end = bookprep.parse_period("2023-01")
            first = bookprep.inspect_source_file(path=root / "a" / "transactions.csv", root_dir=root, period_start=period_start, period_end=period_end)
            second = bookprep.inspect_source_file(path=root / "b" / "transactions.csv", root_dir=root, period_start=period_start, period_end=period_end)

            assert first is not None and second is not None
            self.assertNotEqual(first.source_id, second.source_id)
            self.assertIn("a-transactions-csv", first.source_id)
            self.assertIn("b-transactions-csv", second.source_id)

    def test_inspect_sources_ignores_general_markdown_readme_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# notes\n", encoding="utf-8")
            (root / "woo.csv").write_text(
                "Date,Orders,Gross sales,Returns,Coupons,Net sales,Taxes,Shipping,Total sales\n"
                "2024-01-15 00:00:00,1,10.00,0.00,0.00,10.00,2.00,0.00,12.00\n",
                encoding="utf-8",
            )

            period_start, period_end = bookprep.parse_period("2024-01")
            sources = bookprep.inspect_sources(
                source_dir=root,
                root_dir=root,
                period_start=period_start,
                period_end=period_end,
            )

            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].source_type, "csv")
            self.assertEqual(sources[0].source_system, "woo")

    def test_printful_no_activity_marker_is_canonical_zero_activity_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "2025-pack" / "Printful" / "no-activity-during-period"
            marker.parent.mkdir(parents=True)
            marker.touch()
            period_start, period_end = bookprep.parse_period("2025-01")

            sources = bookprep.inspect_sources(
                source_dir=root,
                root_dir=root,
                period_start=period_start,
                period_end=period_end,
            )
            records, exceptions = bookprep.aggregate_results(
                sources=sources,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual(len(sources), 1)
            self.assertTrue(sources[0].canonical)
            self.assertEqual(sources[0].source_system, "printful")
            self.assertEqual(sources[0].parser_name, "parse_no_activity_marker")
            self.assertEqual(sources[0].covered_from, date(2025, 1, 1))
            self.assertEqual(sources[0].covered_until, date(2025, 12, 31))
            self.assertFalse(any(item["blocking"] for item in exceptions))
            self.assertFalse(any(records.values()))

    def test_printful_no_activity_marker_without_period_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "Printful" / "no-activity-during-period"
            marker.parent.mkdir()
            marker.touch()
            period_start, period_end = bookprep.parse_period("2025-01")

            sources = bookprep.inspect_sources(
                source_dir=root,
                root_dir=root,
                period_start=period_start,
                period_end=period_end,
            )
            records, exceptions = bookprep.aggregate_results(
                sources=sources,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual(sources[0].parser_name, "unrecognized_source")
            self.assertTrue(any(item["blocking"] for item in exceptions))
            self.assertFalse(any(records.values()))

    def test_printful_monthly_no_activity_marker_does_not_cover_other_months(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "2025-01" / "Printful" / "no-activity-during-period"
            marker.parent.mkdir(parents=True)
            marker.touch()
            period_start, period_end = bookprep.parse_period("2025-11")

            sources = bookprep.inspect_sources(
                source_dir=root,
                root_dir=root,
                period_start=period_start,
                period_end=period_end,
            )

            self.assertFalse(sources)

    def test_printful_marker_ignores_year_in_ancestor_above_source_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="work-2025-") as tmp:
            source_root = Path(tmp) / "source"
            marker = source_root / "Printful" / "no-activity-during-period"
            marker.parent.mkdir(parents=True)
            marker.touch()
            period_start, period_end = bookprep.parse_period("2025-01")

            sources = bookprep.inspect_sources(
                source_dir=source_root,
                root_dir=source_root,
                period_start=period_start,
                period_end=period_end,
            )

            self.assertEqual(sources[0].parser_name, "unrecognized_source")

    def test_parse_woo_sales_csv_adds_sales_record_and_returns_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "woo_2023-01.csv"
            csv_path.write_text(
                "Date,Orders,Gross sales,Returns,Coupons,Net sales,Taxes,Shipping,Total sales\n"
                "2023-01-15 00:00:00,2,100.00,10.00,0.00,90.00,18.00,5.00,113.00\n",
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2023-01")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_woo_sales_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual(len(records["sales"]), 1)
            self.assertEqual(records["sales"][0]["shipping_amount"], 5.0)
            self.assertEqual(records["sales"][0]["vat_amount"], 18.0)
            self.assertTrue(any("returns" in item["reason"].lower() for item in exceptions))

    def test_parse_woo_monthly_summary_csv_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "woocommerce-sales-report.csv"
            csv_path.write_text(
                '"Date","Number of items sold","Number of orders","Average net sales amount","Coupon amount","Shipping amount","Gross sales amount","Net sales amount","Refund amount"\n'
                '"2024-10","2","2","0","0.00","9.70","66.27","50.00","0.00"\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2024-10")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None
            self.assertEqual(source.source_system, "woo")
            self.assertEqual(source.parser_name, "parse_woo_sales_csv")

            records, exceptions = bookprep.parse_woo_sales_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["sales"]), 1)
            sale = records["sales"][0]
            self.assertEqual(sale["event_date"], "2024-10-31")
            self.assertEqual(sale["gross_amount"], 66.27)
            self.assertEqual(sale["net_amount"], 50.0)
            self.assertEqual(sale["shipping_amount"], 9.7)
            self.assertEqual(sale["vat_amount"], 6.57)
            self.assertEqual(sale["quantity"], 2.0)
            self.assertEqual(sale["attributes"]["orders"], 2)
            self.assertTrue(sale["attributes"]["is_monthly_summary"])

    def test_parse_bank_csv_creates_signed_bank_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "kontovv_2023.csv"
            csv_path.write_text(
                "Kliendi konto,Dokumendi number,Kuupäev,Saaja/maksja konto,Saaja/maksja nimi,Deebet/Kreedit (D/C),Summa,Viitenumber,Arhiveerimistunnus,Selgitus,Valuuta\n"
                "\"EE00\",\"DOC1\",2023-01-02,\"ACC1\",\"STRIPE\",C,126.60,\"\",\"ARCH1\",\"STRIPE\",\"EUR\"\n"
                "\"EE00\",\"DOC2\",2023-01-03,\"\",\"Printful\",D,-26.62,\"\",\"ARCH2\",\"Printful charge\",\"EUR\"\n",
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2023-01")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, _ = bookprep.parse_bank_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual(len(records["bank_transactions"]), 2)
            amounts = [record["gross_amount"] for record in records["bank_transactions"]]
            self.assertEqual(amounts, [126.6, -26.62])

    def test_parse_paypal_csv_classifies_sales_refunds_and_payouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "paypal_2023_report.CSV"
            csv_path.write_text(
                "Date,Time,TimeZone,Name,Type,Status,Currency,Gross,Fee,Net,Transaction ID,Shipping and Handling Amount,Sales Tax,Country Code\n"
                "01/01/2023,14:04:01,PST,Buyer,Website Payment,Completed,EUR,\"35,82\",\"-1,57\",\"34,25\",TX1,\"0,00\",\"0,00\",DE\n"
                "05/01/2023,14:04:01,PST,Buyer,Refund,Completed,EUR,\"-35,82\",\"0,00\",\"-35,82\",TX2,\"0,00\",\"0,00\",DE\n"
                "07/01/2023,14:04:01,PST,PayPal,General Withdrawal,Completed,EUR,\"-34,25\",\"0,00\",\"-34,25\",TX3,\"0,00\",\"0,00\",DE\n",
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2023-01")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, _ = bookprep.parse_paypal_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual(len(records["sales"]), 1)
            self.assertEqual(len(records["refunds"]), 1)
            self.assertEqual(len(records["payouts"]), 1)
            self.assertEqual(records["sales"][0]["fee_amount"], 1.57)
            self.assertEqual(records["payouts"][0]["gross_amount"], 34.25)

    def test_parse_paypal_csv_keeps_card_funding_and_conversions_out_of_payouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "paypal_2025_report.CSV"
            csv_path.write_text(
                "Date,Name,Type,Status,Currency,Gross,Fee,Net,Transaction ID,Balance Impact\n"
                "09/10/2025,,General Card Withdrawal,Completed,EUR,-13.27,0.00,-13.27,TX1,Debit\n"
                "09/10/2025,,General Card Deposit,Completed,EUR,13.42,0.00,13.42,TX2,Credit\n"
                "09/10/2025,,General Currency Conversion,Completed,USD,-14.94,0.00,-14.94,TX3,Debit\n"
                "09/10/2025,Quartermaster Logistics LLC,General Payment,Completed,USD,-14.94,0.00,-14.94,TX4,Debit\n",
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-10")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_paypal_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["payouts"]), 1)
            self.assertEqual(records["payouts"][0]["external_ref"], "TX1")
            self.assertEqual(len(records["other"]), 3)
            self.assertFalse(records["sales"])
            self.assertFalse(records["refunds"])

    def test_inspect_source_file_infers_quartermaster_sales_month_from_parent_year(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "2024" / "Quartermaster" / "qm_sales_10.pdf"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"%PDF-1.4\n")

            period_start, period_end = bookprep.parse_period("2024-10")
            source = bookprep.inspect_source_file(path=path, root_dir=root, period_start=period_start, period_end=period_end)

            assert source is not None
            self.assertEqual(source.source_system, "quartermaster")
            self.assertEqual(source.covered_from.isoformat(), "2024-10-01")
            self.assertEqual(source.covered_until.isoformat(), "2024-10-31")
            self.assertEqual(source.parser_name, "parse_quartermaster_pdf")

    def test_parse_quartermaster_orders_csv_preserves_order_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "orderHistory_2024.csv"
            csv_path.write_text(
                "ReferenceID,QMLOrderID,Email,Name,Status,OrderType,Carrier,ShippingType,TrackingNumber,DateSubmitted,DateShipped\n"
                "#1501,9146662,sales@qmdirect.com,QM Direct,Shipped,General Fulfillment,Freight,,31427699,10/31/2024 06:58 am,11/14/2024\n",
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2024-10")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_quartermaster_orders_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["other"]), 1)
            order = records["other"][0]
            self.assertEqual(order["event_type"], "quartermaster_order_history")
            self.assertEqual(order["event_date"], "2024-10-31")
            self.assertEqual(order["settlement_date"], "2024-11-14")
            self.assertEqual(order["external_ref"], "#1501")
            self.assertEqual(order["attributes"]["qml_order_id"], "9146662")
            self.assertEqual(order["channel"], "quartermaster")

    def test_parse_stripe_balance_csv_classifies_sales_and_payouts_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "balance_history_stripe.csv"
            csv_path.write_text(
                "\"id\",\"Type\",\"Source\",\"Amount\",\"Fee\",\"Net\",\"Currency\",\"Created (UTC)\",\"Available On (UTC)\",\"tax_amount (metadata)\",\"order_id (metadata)\",\"customer_email (metadata)\",\"customer_name (metadata)\"\n"
                "\"txn_charge_1\",\"charge\",\"ch_1\",\"35.82\",\"0.79\",\"35.03\",\"eur\",\"2023-01-20 18:12\",\"2023-01-25 00:00\",\"0.00\",\"706\",\"buyer@example.com\",\"Buyer\"\n"
                "\"txn_payout_1\",\"payout\",\"po_1\",\"-126.60\",\"0.00\",\"-126.60\",\"eur\",\"2023-01-02 01:15\",\"2023-01-02 01:15\",\"0.00\",,,\n"
                "\"txn_charge_1\",\"charge\",\"ch_1\",\"35.82\",\"0.79\",\"35.03\",\"eur\",\"2023-01-20 18:12\",\"2023-01-25 00:00\",\"0.00\",\"706\",\"buyer@example.com\",\"Buyer\"\n",
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2023-01")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_stripe_balance_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual(len(records["sales"]), 1)
            self.assertEqual(len(records["payouts"]), 1)
            self.assertEqual(records["sales"][0]["fee_amount"], 0.79)
            self.assertEqual(records["sales"][0]["net_amount"], 35.03)
            self.assertEqual(records["payouts"][0]["gross_amount"], 126.6)
            self.assertTrue(any("duplicate" in item["reason"].lower() for item in exceptions))

    def test_parse_stripe_payouts_history_csv_maps_paid_payout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "stripe_payouts_history.csv"
            csv_path.write_text(
                '"payout_id","effective_at_utc","effective_at","currency","gross","fee","net","reporting_category","balance_transaction_id","description","payout_expected_arrival_date","payout_status","payout_reversed_at_utc","payout_reversed_at","payout_type","payout_description","payout_destination_id","trace_id","trace_id_status","application_fee"\n'
                '"po_1","2025-01-02 00:00:57","2025-01-02 02:00:57","eur","67.15","0.00","67.15","payout","txn_1","STRIPE PAYOUT","2025-01-02 00:00:00","paid",,,"bank_account","STRIPE PAYOUT","ba_1","STRIPE-TRACE-1","supported",""\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-01")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None
            source.canonical = True

            try:
                records, exceptions = bookprep.aggregate_results(
                    sources=[source],
                    period_start=period_start,
                    period_end=period_end,
                    base_currency="EUR",
                )
            except Exception as exc:  # the unsupported format is the failing behavior under test
                self.fail(f"Stripe payouts history format was not parsed: {exc}")

            self.assertEqual(source.parser_name, "parse_stripe_payouts_csv")
            self.assertFalse([item for item in exceptions if item["blocking"]])
            self.assertEqual(len(records["payouts"]), 1)
            payout = records["payouts"][0]
            self.assertEqual(payout["event_date"], "2025-01-02")
            self.assertEqual(payout["settlement_date"], "2025-01-02")
            self.assertEqual(payout["gross_amount"], 67.15)
            self.assertEqual(payout["net_amount"], 67.15)
            self.assertEqual(payout["external_ref"], "po_1")
            self.assertEqual(payout["attributes"]["stripe_balance_transaction_id"], "txn_1")
            self.assertEqual(payout["attributes"]["trace_id"], "STRIPE-TRACE-1")

    def test_parse_stripe_payouts_history_csv_blocks_unsettled_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "stripe_payouts_history.csv"
            csv_path.write_text(
                '"payout_id","effective_at_utc","currency","gross","fee","net","reporting_category","balance_transaction_id","payout_expected_arrival_date","payout_status","payout_reversed_at_utc"\n'
                '"po_failed","2025-01-02 00:00:57","eur","10","0","10","payout","txn_failed","2025-01-02 00:00:00","failed",""\n'
                '"po_canceled","2025-01-03 00:00:57","eur","11","0","11","payout","txn_canceled","2025-01-03 00:00:00","canceled",""\n'
                '"po_pending","2025-01-04 00:00:57","eur","12","0","12","payout","txn_pending","2025-01-04 00:00:00","pending",""\n'
                '"po_unknown","2025-01-05 00:00:57","eur","13","0","13","payout","txn_unknown","2025-01-05 00:00:00","review",""\n'
                '"po_reversed","2025-01-06 00:00:57","eur","14","0","14","payout","txn_reversed","2025-01-06 00:00:00","paid","2025-01-07 00:00:00"\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-01")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None
            source.canonical = True

            try:
                records, exceptions = bookprep.aggregate_results(
                    sources=[source],
                    period_start=period_start,
                    period_end=period_end,
                    base_currency="EUR",
                )
            except Exception as exc:
                self.fail(f"Stripe payout statuses were not handled conservatively: {exc}")

            self.assertFalse(records["payouts"])
            self.assertEqual(sum(bool(item["blocking"]) for item in exceptions), 3)
            self.assertEqual(sum("skipped-status" in item["exception_id"] for item in exceptions), 2)

    def test_parse_stripe_charges_csv_maps_needed_columns_and_emits_refund(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "stripe_balance_history.csv"
            csv_path.write_text(
                '"id","Created date (UTC)","Amount","Amount Refunded","Currency","Description","Fee","PaymentIntent ID","Refunded date (UTC)","Status","order_id (metadata)","customer_email (metadata)","customer_name (metadata)","tax_amount (metadata)"\n'
                '"ch_paid","2025-04-20 07:03:21","35.82","0.00","eur","Order 701","0.79","pi_paid","","Paid","701","buyer@example.com","Buyer","0.00"\n'
                '"ch_refunded","2025-05-02 10:00:00","10.00","10.00","eur","Order 702","0.50","pi_refunded","2025-05-03 11:00:00","Refunded","702","refund@example.com","Refund Buyer","657"\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-05")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_stripe_balance_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["sales"]), 1)
            self.assertEqual(len(records["refunds"]), 1)
            sale = records["sales"][0]
            self.assertEqual(sale["event_type"], "stripe_charge")
            self.assertEqual(sale["event_date"], "2025-05-02")
            self.assertEqual(sale["settlement_date"], "2025-05-02")
            self.assertEqual(sale["gross_amount"], 10.0)
            self.assertEqual(sale["fee_amount"], 0.5)
            self.assertEqual(sale["net_amount"], 9.5)
            self.assertEqual(sale["vat_amount"], 6.57)
            self.assertEqual(sale["external_ref"], "pi_refunded")
            refund = records["refunds"][0]
            self.assertEqual(refund["event_type"], "stripe_refund")
            self.assertEqual(refund["event_date"], "2025-05-03")
            self.assertEqual(refund["gross_amount"], -10.0)
            self.assertEqual(refund["net_amount"], -10.0)
            self.assertEqual(refund["vat_amount"], -6.57)

    def test_parse_stripe_charges_csv_skips_failed_and_blocks_pending_or_unknown_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "stripe_charges.csv"
            csv_path.write_text(
                '"id","Created date (UTC)","Amount","Amount Refunded","Currency","Fee","PaymentIntent ID","Refunded date (UTC)","Status"\n'
                '"ch_failed","2025-05-01 10:00:00","10.00","0","eur","0.50","pi_failed","","Failed"\n'
                '"ch_pending","2025-05-02 10:00:00","11.00","0","eur","0.50","pi_pending","","Pending"\n'
                '"ch_unknown","2025-05-03 10:00:00","12.00","0","eur","0.50","pi_unknown","","Needs review"\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-05")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_stripe_balance_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertFalse(records["sales"])
            self.assertTrue(any("failed" in item["reason"].lower() and not item["blocking"] for item in exceptions))
            self.assertEqual(sum(bool(item["blocking"]) for item in exceptions), 2)

    def test_parse_stripe_charges_csv_does_not_block_other_month_for_pending_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "stripe_charges.csv"
            csv_path.write_text(
                '"id","Created date (UTC)","Amount","Amount Refunded","Currency","Fee","PaymentIntent ID","Refunded date (UTC)","Status"\n'
                '"ch_pending","2025-04-02 10:00:00","11.00","0","eur","0.50","pi_pending","","Pending"\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-05")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_stripe_balance_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertFalse(any(records.values()))
            self.assertFalse(any(item["blocking"] for item in exceptions))

    def test_parse_stripe_charges_csv_accepts_succeeded_skips_canceled_and_blocks_over_refund(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "stripe_charges.csv"
            csv_path.write_text(
                '"id","Created date (UTC)","Amount","Amount Refunded","Currency","Fee","PaymentIntent ID","Refunded date (UTC)","Status"\n'
                '"ch_ok","2025-05-01 10:00:00","10.00","0","eur","0.50","pi_ok","","Succeeded"\n'
                '"ch_canceled","2025-05-02 10:00:00","11.00","0","eur","0.50","pi_canceled","","Canceled"\n'
                '"ch_bad_refund","2025-05-03 10:00:00","12.00","13.00","eur","0.50","pi_bad","2025-05-04 10:00:00","Refunded"\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-05")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_stripe_balance_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual([record["external_ref"] for record in records["sales"]], ["pi_ok"])
            self.assertTrue(any("canceled" in item["reason"].lower() and not item["blocking"] for item in exceptions))
            self.assertTrue(any("exceeds charge" in item["reason"].lower() and item["blocking"] for item in exceptions))

    def test_parse_stripe_charges_csv_blocks_refund_without_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "stripe_charges.csv"
            csv_path.write_text(
                '"id","Created date (UTC)","Amount","Amount Refunded","Currency","Fee","PaymentIntent ID","Refunded date (UTC)","Status","tax_amount (metadata)"\n'
                '"ch_refund","2025-05-01 10:00:00","10.00","4.00","eur","0.50","pi_refund","","Partially refunded","2.00"\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-05")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_stripe_balance_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual(len(records["sales"]), 1)
            self.assertFalse(records["refunds"])
            self.assertTrue(any("refund date" in item["reason"].lower() and item["blocking"] for item in exceptions))

    def test_parse_stripe_charges_csv_rounds_partial_refund_vat_to_cents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "stripe_charges.csv"
            csv_path.write_text(
                '"id","Created date (UTC)","Amount","Amount Refunded","Currency","Fee","PaymentIntent ID","Refunded date (UTC)","Status","tax_amount (metadata)"\n'
                '"ch_refund","2025-05-01 10:00:00","3.00","1.00","eur","0.10","pi_refund","2025-05-02 10:00:00","Partially refunded","100"\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-05")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_stripe_balance_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertFalse(exceptions)
            self.assertEqual(records["refunds"][0]["vat_amount"], -0.33)

    def test_aggregate_prefers_charges_sales_but_retains_balance_history_payouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stripe_charges.csv").write_text(
                '"id","Created date (UTC)","Amount","Amount Refunded","Currency","Fee","PaymentIntent ID","Refunded date (UTC)","Status"\n'
                '"ch_1","2025-05-01 10:00:00","10.00","0","eur","0.50","pi_1","","Paid"\n',
                encoding="utf-8",
            )
            (root / "stripe_balance_history.csv").write_text(
                '"id","Type","Source","Amount","Fee","Net","Currency","Created (UTC)","Available On (UTC)"\n'
                '"txn_1","charge","ch_1","10.00","0.50","9.50","eur","2025-05-01 10:00","2025-05-03 00:00"\n'
                '"txn_fee","fee","fee_1","-0.50","0.50","-0.50","eur","2025-05-01 10:00","2025-05-03 00:00"\n'
                '"txn_payout","payout","po_1","-9.50","0","-9.50","eur","2025-05-04 10:00","2025-05-04 10:00"\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-05")
            sources = bookprep.inspect_sources(
                source_dir=root,
                root_dir=root,
                period_start=period_start,
                period_end=period_end,
            )

            records, exceptions = bookprep.aggregate_results(
                sources=sources,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual(len(records["sales"]), 1)
            self.assertEqual(records["sales"][0]["attributes"]["stripe_export_type"], "charges")
            self.assertEqual(len(records["payouts"]), 1)
            self.assertEqual(len(records["fees"]), 1)
            self.assertTrue(any("superseded" in item["reason"].lower() for item in exceptions))

    def test_aggregate_prefers_payouts_history_duplicate_but_retains_balance_history_fee(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stripe_payouts_history.csv").write_text(
                '"payout_id","effective_at_utc","currency","gross","fee","net","reporting_category","balance_transaction_id","payout_expected_arrival_date","payout_status"\n'
                '"po_1","2025-05-04 10:00:00","eur","9.50","0","9.50","payout","txn_payout","2025-05-04 10:00:00","paid"\n',
                encoding="utf-8",
            )
            (root / "stripe_balance_history.csv").write_text(
                '"id","Type","Source","Amount","Fee","Net","Currency","Created (UTC)","Available On (UTC)"\n'
                '"txn_fee","fee","fee_1","-0.50","0.50","-0.50","eur","2025-05-01 10:00","2025-05-03 00:00"\n'
                '"txn_payout","payout","po_1","-9.50","0","-9.50","eur","2025-05-04 10:00","2025-05-04 10:00"\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-05")
            sources = bookprep.inspect_sources(
                source_dir=root,
                root_dir=root,
                period_start=period_start,
                period_end=period_end,
            )

            records, exceptions = bookprep.aggregate_results(
                sources=sources,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual(len(records["payouts"]), 1)
            self.assertEqual(records["payouts"][0]["attributes"]["stripe_export_type"], "payouts_history")
            self.assertEqual(len(records["fees"]), 1)
            self.assertTrue(any("payout" in item["reason"].lower() and "superseded" in item["reason"].lower() for item in exceptions))

    def test_aggregate_retains_same_value_payouts_when_immutable_ids_differ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stripe_payouts_history.csv").write_text(
                '"payout_id","effective_at_utc","currency","gross","fee","net","reporting_category","balance_transaction_id","payout_expected_arrival_date","payout_status"\n'
                '"po_new","2025-05-04 10:00:00","eur","9.50","0","9.50","payout","txn_new","2025-05-04 10:00:00","paid"\n',
                encoding="utf-8",
            )
            (root / "stripe_balance_history.csv").write_text(
                '"id","Type","Source","Amount","Fee","Net","Currency","Created (UTC)","Available On (UTC)"\n'
                '"txn_other","payout","po_other","-9.50","0","-9.50","eur","2025-05-04 10:00","2025-05-04 10:00"\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-05")
            sources = bookprep.inspect_sources(
                source_dir=root,
                root_dir=root,
                period_start=period_start,
                period_end=period_end,
            )

            records, exceptions = bookprep.aggregate_results(
                sources=sources,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual(len(records["payouts"]), 2)
            self.assertTrue(any("possible duplicate" in item["reason"].lower() for item in exceptions))

    def test_aggregate_retains_distinct_stripe_charges_with_same_date_and_amount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stripe_charges.csv").write_text(
                '"id","Created date (UTC)","Amount","Amount Refunded","Currency","Fee","PaymentIntent ID","Refunded date (UTC)","Status"\n'
                '"ch_1","2025-05-01 10:00:00","10.00","0","eur","0.50","pi_1","","Paid"\n',
                encoding="utf-8",
            )
            (root / "stripe_balance_history.csv").write_text(
                '"id","Type","Source","Amount","Fee","Net","Currency","Created (UTC)","Available On (UTC)"\n'
                '"txn_2","charge","ch_2","10.00","0.50","9.50","eur","2025-05-01 10:00","2025-05-03 00:00"\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-05")
            sources = bookprep.inspect_sources(
                source_dir=root,
                root_dir=root,
                period_start=period_start,
                period_end=period_end,
            )

            records, exceptions = bookprep.aggregate_results(
                sources=sources,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual(len(records["sales"]), 2)
            self.assertTrue(any("possible duplicate" in item["reason"].lower() for item in exceptions))

    def test_aggregate_does_not_dedupe_distinct_charges_for_same_order_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stripe_charges.csv").write_text(
                '"id","Created date (UTC)","Amount","Amount Refunded","Currency","Fee","PaymentIntent ID","Refunded date (UTC)","Status","order_id (metadata)"\n'
                '"ch_1","2025-05-01 10:00:00","10.00","0","eur","0.50","pi_1","","Paid","42"\n',
                encoding="utf-8",
            )
            (root / "stripe_balance_history.csv").write_text(
                '"id","Type","Source","Amount","Fee","Net","Currency","Created (UTC)","Available On (UTC)","order_id (metadata)"\n'
                '"txn_2","charge","ch_2","20.00","0.50","19.50","eur","2025-05-02 10:00","2025-05-03 00:00","42"\n',
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2025-05")
            sources = bookprep.inspect_sources(
                source_dir=root,
                root_dir=root,
                period_start=period_start,
                period_end=period_end,
            )

            records, exceptions = bookprep.aggregate_results(
                sources=sources,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual(len(records["sales"]), 2)
            self.assertTrue(any("possible duplicate" in item["reason"].lower() for item in exceptions))

    def test_parse_stripe_invoice_pdf_creates_explicit_fee_record_for_service_month(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = pdf_source(root, "stripe-fee.pdf", source_system="stripe")
            pages = [
                "Tax Invoice\n"
                "Invoice Number\nCWZ5RHUU-2023-01\n"
                "Invoice Date\nFeb 1, 2023\n"
                "Service Month\nJan 2023\n"
                "Stripe Processing Fees\n2 card payments totaling €62.70\n"
                "Stripe Fees\n€2.32\n"
                "Total VAT\n€0.00\n"
                "Total\n€2.32\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_stripe_invoice_pdf(
                    source,
                    period_start=date(2023, 1, 1),
                    period_end=date(2023, 1, 31),
                    base_currency="EUR",
                )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["fees"]), 1)
            fee = records["fees"][0]
            self.assertEqual(fee["event_date"], "2023-01-31")
            self.assertEqual(fee["settlement_date"], "2023-02-01")
            self.assertEqual(fee["gross_amount"], 2.32)
            self.assertEqual(fee["fee_amount"], 2.32)
            self.assertEqual(fee["channel"], "stripe")

    def test_parse_quartermaster_sales_report_pdf_creates_sales_and_fee_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = pdf_source(root, "qm_sales_10.pdf", source_system="quartermaster")
            pages = [
                "Sales Report\n"
                "Date\n10/31/2024\n"
                "S.R. No.\n1174\n"
                "Vendor\nPlepic Games LLC\n"
                "Quartermaster Direct\n"
                "This represents your sales report for October 2024\n"
                "Total\n"
                "Item Description Qty Rate Amount\n"
                "Lunar Base PPG01000 - Lunar Base - 706189519318 - China - Sold\n"
                "Copies\n"
                "82 9.66 792.12\n"
                "Picking Fee QML Picking Fee - $.40 per unit 82 -0.40 -32.80\n"
                "$759.32\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_quartermaster_pdf(
                    source,
                    period_start=date(2024, 10, 1),
                    period_end=date(2024, 10, 31),
                    base_currency="EUR",
                )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["sales"]), 1)
            self.assertEqual(len(records["fees"]), 1)
            sale = records["sales"][0]
            fee = records["fees"][0]
            self.assertEqual(sale["currency"], "USD")
            self.assertEqual(sale["gross_amount"], 792.12)
            self.assertEqual(sale["quantity"], 82.0)
            self.assertEqual(sale["external_ref"], "1174")
            self.assertEqual(fee["currency"], "USD")
            self.assertEqual(fee["gross_amount"], 32.8)
            self.assertEqual(fee["fee_amount"], 32.8)
            self.assertEqual(fee["channel"], "quartermaster")

    def test_parse_quartermaster_sales_report_accepts_short_date_and_sums_product_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = pdf_source(root, "qm_sales_01.pdf", source_system="quartermaster")
            pages = [
                "Sales Report\n"
                "Date\n1/31/2025\n"
                "S.R. No.\n1504\n"
                "Vendor\nPlepic Games LLC\n"
                "Quartermaster Direct\n"
                "This represents your sales report for January 2025\n"
                "Total\nItem Description Qty Rate Amount\n"
                "Lunar Base retail - Sold\nCopies\n43 9.66 415.38\n"
                "Lunar Base demo - Sold Copies\n1 5.00 5.00\n"
                "Picking Fee QML Picking Fee - $.40 per unit 44 -0.40 -17.60\n"
                "$402.78\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_quartermaster_pdf(
                    source,
                    period_start=date(2025, 1, 1),
                    period_end=date(2025, 1, 31),
                    base_currency="EUR",
                )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["sales"]), 1)
            self.assertEqual(records["sales"][0]["event_date"], "2025-01-31")
            self.assertEqual(records["sales"][0]["quantity"], 44.0)
            self.assertEqual(records["sales"][0]["gross_amount"], 420.38)
            self.assertEqual(records["fees"][0]["gross_amount"], 17.6)

    def test_inspect_quartermaster_pdf_uses_report_date_over_filename_month(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "2025-pack" / "Quartermaster"
            root.mkdir(parents=True)
            path = root / "qm_sales_12.pdf"
            path.write_bytes(b"%PDF-1.4\n")
            pages = ["Sales Report\nDate\n7/31/2025\nQuartermaster Direct\n"]
            period_start, period_end = bookprep.parse_period("2025-07")

            with (
                mock.patch.object(bookprep, "PdfReader", object()),
                mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages),
            ):
                source = bookprep.inspect_source_file(
                    path=path,
                    root_dir=Path(tmp),
                    period_start=period_start,
                    period_end=period_end,
                )

            assert source is not None
            self.assertEqual(source.covered_from, date(2025, 7, 31))
            self.assertEqual(source.covered_until, date(2025, 7, 31))

    def test_parse_quartermaster_zero_sales_report_is_valid_zero_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = pdf_source(root, "qm_sales_07.pdf", source_system="quartermaster")
            pages = [
                "Sales Report\nDate\n8/31/2025\nS.R. No.\n2385\n"
                "Vendor\nPlepic Games LLC\nQuartermaster Direct\n"
                "This represents your Sales Report for August 2025. NOTE: No sales for this pay period.\n"
                "Lunar Base - Sold Copies\n0 5.00 0.00\n"
                "Picking Fee QML Picking Fee - $.40 per unit 0 -0.40 0.00\n$0.00\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_quartermaster_pdf(
                    source,
                    period_start=date(2025, 8, 1),
                    period_end=date(2025, 8, 31),
                    base_currency="EUR",
                )

            self.assertFalse(exceptions)
            self.assertFalse(any(records.values()))

    def test_parse_quartermaster_invoice_pdf_creates_usd_purchase_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = pdf_source(root, "2024.10.01_Quartermaster.pdf", source_system="quartermaster")
            pages = [
                "INVOICE\n"
                "Quartermaster Logistics LLC\n"
                "Invoice Date\n"
                "10/01/2024\n"
                "Invoice Due Date\n"
                "10/25/2024\n"
                "Invoice Number\n"
                "00635-00001\n"
                "Invoice Total\n"
                "$ 31.00\n"
                "Notes\n"
                "September Monthly Invoice\n"
                "Description Type Amount\n"
                "Receiving Debit $ 10.00\n"
                "Storage Debit $ 21.00\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_quartermaster_pdf(
                    source,
                    period_start=date(2024, 10, 1),
                    period_end=date(2024, 10, 31),
                    base_currency="EUR",
                )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["purchase_expenses"]), 1)
            purchase = records["purchase_expenses"][0]
            self.assertEqual(purchase["currency"], "USD")
            self.assertEqual(purchase["gross_amount"], 31.0)
            self.assertEqual(purchase["net_amount"], 31.0)
            self.assertEqual(purchase["event_type"], "quartermaster_service_invoice")
            self.assertEqual(purchase["external_ref"], "00635-00001")
            self.assertIn("September Monthly Invoice", purchase["description"])

    def test_infer_pdf_source_system_keeps_consignee_mentions_generic(self) -> None:
        text = (
            "ARVE 127434\n"
            "BALTI LOGISTIKA AS\n"
            "Kokku tasuda: 861.24 EUR\n"
            "Saaja: Plepic Games c/o Quartermaster Logistics LLC\n"
        )
        self.assertEqual(bookprep.infer_pdf_source_system(text, "document"), "document")

    def test_parse_purchase_invoice_pdf_extracts_dpd_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = pdf_source(root, "2024.04.20_DPD.pdf", source_system="document")
            pages = [
                "DPD EESTI AS\n"
                "Plepic Games OÜ Arve number 40534576\n"
                "Arve kuupäev 20.04.2024\n"
                "Maksetähtaeg 20.04.2024\n"
                "Kogusumma KM-ga\n"
                "22.00 35.00 EUR 0.00 EUR 35.00 EUR 7.70 EUR 42.70 EUR\n"
                "Summa 35.00 EUR 0.00 EUR 35.00 EUR 7.70 EUR 42.70 EUR\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_purchase_invoice_pdf(
                    source,
                    period_start=date(2024, 4, 1),
                    period_end=date(2024, 4, 30),
                    base_currency="EUR",
                )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["purchase_expenses"]), 1)
            expense = records["purchase_expenses"][0]
            self.assertEqual(expense["gross_amount"], 42.7)
            self.assertEqual(expense["net_amount"], 35.0)
            self.assertEqual(expense["vat_amount"], 7.7)
            self.assertEqual(expense["external_ref"], "40534576")
            self.assertEqual(expense["attributes"]["vendor_name"], "DPD EESTI AS")

    def test_parse_purchase_invoice_pdf_extracts_omniva_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = pdf_source(root, "2024.10.15_Omniva.pdf", source_system="document")
            pages = [
                "Arve\n"
                "A000048795\n"
                "Arve kuupäev\n"
                "15.10.2024\n"
                "Ridade summa\n"
                "Käibemaks\n"
                "Ümardus\n"
                "Kokku\n"
                "EUR\n"
                "31,25\n"
                "6,88\n"
                "0,00\n"
                "38,13\n"
                "AS Eesti Post\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_purchase_invoice_pdf(
                    source,
                    period_start=date(2024, 10, 1),
                    period_end=date(2024, 10, 31),
                    base_currency="EUR",
                )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["purchase_expenses"]), 1)
            expense = records["purchase_expenses"][0]
            self.assertEqual(expense["gross_amount"], 38.13)
            self.assertEqual(expense["net_amount"], 31.25)
            self.assertEqual(expense["vat_amount"], 6.88)
            self.assertEqual(expense["external_ref"], "A000048795")
            self.assertEqual(expense["attributes"]["vendor_name"], "AS Eesti Post")

    def test_parse_purchase_invoice_pdf_extracts_balti_logistika_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = pdf_source(root, "2024.08.28_Balti_Logistika.pdf", source_system="document")
            pages = [
                "ARVE 127434\n"
                "Arve kuupäev: 28.08.2024\n"
                "BALTI LOGISTIKA AS\n"
                "Euroopa Keskpanga valuutakursid: 852.0 EUR 9.24 EUR 861.24 EUR\n"
                "1 Käibemaks on arvestatud vastavalt KMS § 15 lg 4 p 9 Kokku tasuda: 861.24 EUR\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_purchase_invoice_pdf(
                    source,
                    period_start=date(2024, 8, 1),
                    period_end=date(2024, 8, 31),
                    base_currency="EUR",
                )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["purchase_expenses"]), 1)
            expense = records["purchase_expenses"][0]
            self.assertEqual(expense["gross_amount"], 861.24)
            self.assertEqual(expense["net_amount"], 852.0)
            self.assertEqual(expense["vat_amount"], 9.24)
            self.assertEqual(expense["attributes"]["vendor_name"], "BALTI LOGISTIKA AS")

    def test_parse_purchase_invoice_pdf_extracts_jajaa_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = pdf_source(root, "2025.10.30_JaJaa.pdf", source_system="document")
            pages = [
                "Kuupäev:\nTingimused:\nTähtaeg:\n5 päeva\n04.11.2025\n"
                "Jajaa OÜ\nTatari 8/Sakala 22\n"
                "11,30 20 Värviprint A4 300g 105\n"
                "Plepic Games OÜ\nMaksja:\nJajaa OÜ\n30.10.2025\n"
                "2505331 Arve nr.:\nEE100265839\n"
                "KM-ta\n11,30 24%\nKM% KMsumma\n2,71\nKokku\n14,01\n"
                "Tasumisel märkige palun arve number\nSWIFT: HABAEE2X\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_purchase_invoice_pdf(
                    source,
                    period_start=date(2025, 10, 1),
                    period_end=date(2025, 10, 31),
                    base_currency="EUR",
                )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["purchase_expenses"]), 1)
            expense = records["purchase_expenses"][0]
            self.assertEqual(expense["event_date"], "2025-10-30")
            self.assertEqual(expense["external_ref"], "2505331")
            self.assertEqual(expense["gross_amount"], 14.01)
            self.assertEqual(expense["net_amount"], 11.3)
            self.assertEqual(expense["vat_amount"], 2.71)

    def test_ostuarved_readme_note_becomes_canonical_purchase_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ostuarved = root / "Ostuarved"
            ostuarved.mkdir()
            (ostuarved / "2024.04.23_Omniva_Hanno.jpg").write_bytes(b"jpg")
            (ostuarved / "README.md").write_text(
                "2024.04.23 Omniva, paid by Hanno:\n"
                "15.80€ * 4 + 18.90€ = 82.10€; VAT 0% (postal service)\n",
                encoding="utf-8",
            )

            period_start, period_end = bookprep.parse_period("2024-04")
            sources = bookprep.inspect_sources(
                source_dir=root,
                root_dir=root,
                period_start=period_start,
                period_end=period_end,
            )

            manual_source = next(source for source in sources if source.source_type == "manual")
            image_source = next(source for source in sources if source.path.suffix.lower() == ".jpg")
            self.assertTrue(manual_source.canonical)
            self.assertFalse(image_source.canonical)
            self.assertEqual(manual_source.canonical_group, image_source.canonical_group)

            records, exceptions = bookprep.aggregate_results(
                sources=sources,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertFalse([item for item in exceptions if item.get("blocking")])
            self.assertEqual(len(records["purchase_expenses"]), 1)
            expense = records["purchase_expenses"][0]
            self.assertEqual(expense["gross_amount"], 82.1)
            self.assertEqual(expense["vat_amount"], 0.0)
            self.assertEqual(expense["attributes"]["vendor_name"], "Omniva")
            self.assertTrue(expense["attributes"]["manual_note"])

    def test_parse_printful_pdf_creates_monthly_summary_and_storage_invoice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = pdf_source(root, "printful-report.pdf", source_system="printful")
            pages = [
                "VAT report\n"
                "Invoice date: 2023-02-10\n"
                "Invoice: #LV90011218978-EE102137075-EUR-202301\n"
                "Invoice period: Jan 1, 2023 - Jan 31, 2023\n"
                "Invoice summary:\n"
                "Grand total €22.96\n"
                "Warehousing summary:\n"
                "Grand total €10.69\n"
                "Please find invoice details in the invoice attachment.\n"
                "Invoice #83371584\n"
                "Printful Inc.\n"
                "Billing period: Jan 1, 2023-Jan 31, 2023\n"
                "Invoice date: Feb 1, 2023\n"
                "Description Warehouse location Service price VAT rate VAT, EUR Amount, EUR\n"
                "Storage fee for warehoused products €22.00 21% Latvia €4.62 €26.62\n"
                "Total amount €26.62\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_printful_pdf(
                    source,
                    period_start=date(2023, 1, 1),
                    period_end=date(2023, 1, 31),
                    base_currency="EUR",
                )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["purchase_expenses"]), 2)
            totals = sorted(record["gross_amount"] for record in records["purchase_expenses"])
            self.assertEqual(totals, [26.62, 33.65])
            storage = next(record for record in records["purchase_expenses"] if record["external_ref"] == "83371584")
            self.assertEqual(storage["vat_amount"], 4.62)
            self.assertEqual(storage["event_date"], "2023-01-31")

    def test_parse_printful_orders_csv_nets_same_period_refunds_and_normalizes_refund_only_credit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "Orders.csv"
            csv_path.write_text(
                "Date,Order,Printful ID,Shipped from,Shipped to,State,Payment Instrument,Status,Products,Discount,Shipping,Digitization,Branding,Fulfillment fees,Tax,VAT,Total\n"
                "\"July 10, 2024\",\"Order 792\",108062367,LV,Italy,,\"Printful Wallet\",Completed,€0.00,€0.00,€5.29,€0.00,€0.00,€2.61,-,-,€7.90\n"
                "\"July 12, 2024\",\"Refund to wallet 792\",108062367,LV,Italy,,\"Printful Wallet\",Refunded,€0.00,€0.00,-€5.29,€0.00,€0.00,-€2.61,-,-,-€7.90\n"
                "\"July 9, 2024\",\"Order 790\",107531681,GB,\"United Kingdom\",,\"Printful Wallet\",Completed,€0.00,€0.00,€3.99,€0.00,€0.00,€2.85,-,€1.37,€8.21\n"
                "\"July 12, 2024\",\"Refund to wallet 791\",108021610,LV,\"United States\",Washington,\"Printful Wallet\",Refunded,€0.00,€0.00,-€8.79,€0.00,€0.00,-€2.61,-,-,-€11.40\n"
                "\"Total paid (€):\",,,,,,,,€0.00,€0.00,€9.28,€0.00,€0.00,€2.85,€0.00,€1.37,€8.21\n",
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2024-07")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_printful_orders_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertEqual(len(records["purchase_expenses"]), 1)
            expense = records["purchase_expenses"][0]
            self.assertEqual(expense["gross_amount"], 8.21)
            self.assertEqual(expense["net_amount"], 6.84)
            self.assertEqual(expense["vat_amount"], 1.37)
            self.assertEqual(expense["warehouse_id"], "GB")
            self.assertEqual(expense["external_ref"], "107531681")
            self.assertFalse(exceptions)
            self.assertEqual(len(records["purchase_credits"]), 1)
            credit = records["purchase_credits"][0]
            self.assertEqual(credit["event_type"], "printful_supplier_credit")
            self.assertEqual(credit["event_date"], "2024-07-12")
            self.assertEqual(credit["gross_amount"], 11.4)
            self.assertEqual(credit["external_ref"], "108021610")
            self.assertEqual(credit["attributes"]["source_gross_amount"], -11.4)

    def test_parse_printful_wallet_csv_maps_cash_directions_and_preserves_currency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "Wallet.csv"
            csv_path.write_text(
                "Date,Action,Payment Instrument,Amount\n"
                "\"June 24, 2024\",\"Deposit to wallet\",\"Credit Card\",€8.21\n"
                "\"June 25, 2024\",\"Withdrawal from wallet\",\"Credit Card\",-€3.55\n"
                "\"June 26, 2024\",\"Deposit to wallet\",\"Credit Card\",$306.32\n"
                "\"Total deposits to wallet (€):\",,,+€8.21\n"
                "\"Total withdrawals from wallet (€):\",,,-€3.55\n",
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2024-06")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_printful_wallet_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["bank_transactions"]), 3)
            amounts = [record["gross_amount"] for record in records["bank_transactions"]]
            currencies = [record["currency"] for record in records["bank_transactions"]]
            self.assertEqual(amounts, [-8.21, 3.55, -306.32])
            self.assertEqual(currencies, ["EUR", "EUR", "USD"])

    def test_parse_printful_other_csv_extracts_monthly_service_charge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "Other.csv"
            csv_path.write_text(
                "Date,Category,Payment Instrument,Status,Amount,Discount,Tax,VAT,Total\n"
                "\"April 1, 2024\",\"Custom Product Keeping\",\"Credit Card\",Completed,€150.00,€0.00,-,€31.50,€181.50\n"
                "\"Total paid (€):\",,,,€150.00,€0.00,€0.00,€31.50,€181.50\n",
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2024-04")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_printful_other_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["purchase_expenses"]), 1)
            expense = records["purchase_expenses"][0]
            self.assertEqual(expense["gross_amount"], 181.5)
            self.assertEqual(expense["net_amount"], 150.0)
            self.assertEqual(expense["vat_amount"], 31.5)
            self.assertEqual(expense["channel"], "printful")

    def test_parse_printful_services_csv_preserves_currency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "Services.csv"
            csv_path.write_text(
                "Date,Action,Payment Instrument,Status,Total\n"
                "\"March 16, 2024\",\"Printful Warehousing & Fulfillment Stock Removal\",\"Printful Wallet\",Completed,$306.32\n"
                "\"Total paid ($):\",,,,$306.32\n",
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2024-03")
            source = bookprep.inspect_source_file(path=csv_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None

            records, exceptions = bookprep.parse_printful_services_csv(
                source,
                period_start=period_start,
                period_end=period_end,
                base_currency="EUR",
            )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["purchase_expenses"]), 1)
            expense = records["purchase_expenses"][0]
            self.assertEqual(expense["gross_amount"], 306.32)
            self.assertEqual(expense["currency"], "USD")

    def test_parse_printful_pdf_skips_monthly_summary_when_orders_csv_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orders_path = root / "Orders.csv"
            orders_path.write_text(
                "Date,Order,Printful ID,Shipped from,Shipped to,State,Payment Instrument,Status,Products,Discount,Shipping,Digitization,Branding,Fulfillment fees,Tax,VAT,Total\n"
                "\"January 19, 2023\",\"Order 765\",102389216,LV,Spain,,\"Printful Wallet\",Completed,€0.00,€0.00,€5.29,€0.00,€0.00,€2.61,-,-,€7.90\n",
                encoding="utf-8",
            )
            period_start, period_end = bookprep.parse_period("2023-01")
            orders_source = bookprep.inspect_source_file(path=orders_path, root_dir=root, period_start=period_start, period_end=period_end)
            assert orders_source is not None
            orders_source.canonical = True

            source = pdf_source(root, "printful-report.pdf", source_system="printful")
            source.canonical = True
            pages = [
                "VAT report\n"
                "Invoice date: 2023-02-10\n"
                "Invoice: #LV90011218978-EE102137075-EUR-202301\n"
                "Invoice period: Jan 1, 2023 - Jan 31, 2023\n"
                "Invoice summary:\n"
                "Grand total €22.96\n"
                "Warehousing summary:\n"
                "Grand total €10.69\n"
                "Please find invoice details in the invoice attachment.\n"
                "Invoice #83371584\n"
                "Printful Inc.\n"
                "Billing period: Jan 1, 2023-Jan 31, 2023\n"
                "Invoice date: Feb 1, 2023\n"
                "Description Warehouse location Service price VAT rate VAT, EUR Amount, EUR\n"
                "Storage fee for warehoused products €22.00 21% Latvia €4.62 €26.62\n"
                "Total amount €26.62\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_printful_pdf(
                    source,
                    period_start=date(2023, 1, 1),
                    period_end=date(2023, 1, 31),
                    base_currency="EUR",
                    sources=[source, orders_source],
                )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["purchase_expenses"]), 1)
            self.assertEqual(records["purchase_expenses"][0]["external_ref"], "83371584")

    def test_parse_printful_pdf_skips_storage_invoice_when_other_csv_has_matching_storage_charge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            other_path = root / "Other.csv"
            other_path.write_text(
                "Date,Category,Payment Instrument,Status,Amount,Discount,Tax,VAT,Total\n"
                "\"August 1, 2023\",\"Custom Product Keeping\",\"Credit Card\",Completed,€150.00,€0.00,-,€31.50,€181.50\n",
                encoding="utf-8",
            )
            other_source = bookprep.inspect_source_file(
                path=other_path,
                root_dir=root,
                period_start=date(2023, 8, 1),
                period_end=date(2023, 8, 31),
            )
            assert other_source is not None
            other_source.canonical = True

            pdf_path = root / "vat_report_LV90011218978-EE102137075-EUR-202308_95347439_96815015.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            source = bookprep.inspect_source_file(
                path=pdf_path,
                root_dir=root,
                period_start=date(2023, 1, 1),
                period_end=date(2023, 12, 31),
            )
            assert source is not None
            source.canonical = True
            pages = [
                "VAT report\n"
                "Invoice date: 2023-09-10\n"
                "Invoice: #LV90011218978-EE102137075-EUR-202308\n"
                "Invoice period: Aug 1, 2023 - Aug 31, 2023\n"
                "Please find invoice details in the invoice attachment.\n"
                "Invoice #95347439\n"
                "Printful Inc.\n"
                "Billing period: Aug 1, 2023-Aug 31, 2023\n"
                "Invoice date: Sep 1, 2023\n"
                "Description Warehouse location Service price VAT rate VAT, EUR Amount, EUR\n"
                "Storage fee for warehoused products €150.00 21% Latvia €31.50 €181.50\n"
                "Total amount €181.50\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_printful_pdf(
                    source,
                    period_start=date(2023, 8, 1),
                    period_end=date(2023, 8, 31),
                    base_currency="EUR",
                    sources=[source, other_source],
                )

            self.assertFalse(exceptions)
            self.assertEqual(records["purchase_expenses"], [])

    def test_parse_printful_pdf_prefers_source_matching_billing_month_for_overlapping_invoice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous_path = root / "vat_report_LV90011218978-EE102137075-EUR-202301_83371584_85131636.pdf"
            current_path = root / "vat_report_LV90011218978-EE102137075-EUR-202302_85131636.pdf"
            previous_path.write_bytes(b"%PDF-1.4\n")
            current_path.write_bytes(b"%PDF-1.4\n")
            previous_source = bookprep.inspect_source_file(
                path=previous_path,
                root_dir=root,
                period_start=date(2023, 1, 1),
                period_end=date(2023, 12, 31),
            )
            current_source = bookprep.inspect_source_file(
                path=current_path,
                root_dir=root,
                period_start=date(2023, 1, 1),
                period_end=date(2023, 12, 31),
            )
            assert previous_source is not None
            assert current_source is not None
            previous_source.canonical = True
            current_source.canonical = True
            pages = [
                "VAT report\n"
                "Please find invoice details in the invoice attachment.\n"
                "Invoice #85131636\n"
                "Printful Inc.\n"
                "Billing period: Feb 1, 2023-Feb 28, 2023\n"
                "Invoice date: Mar 1, 2023\n"
                "Description Warehouse location Service price VAT rate VAT, EUR Amount, EUR\n"
                "Storage fee for warehoused products €22.00 21% Latvia €4.62 €26.62\n"
                "Total amount €26.62\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_printful_pdf(
                    previous_source,
                    period_start=date(2023, 2, 1),
                    period_end=date(2023, 2, 28),
                    base_currency="EUR",
                    sources=[previous_source, current_source],
                )

            self.assertFalse(exceptions)
            self.assertEqual(records["purchase_expenses"], [])

    def test_parse_purchase_invoice_pdf_extracts_generic_supplier_invoice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = pdf_source(root, "simplbooks.pdf", source_system="document")
            pages = [
                "Arve nr EE23111186\n"
                "Kuupäev 18.11.2023\n"
                "Toode/Teenus Hind Kogus Summa KM Kokku\n"
                "SimplBooks raamatupidamistarkvara teenustasu\n"
                "Summa km-ta 20% 169.00\n"
                "KM 20% 33.80\n"
                "Arve kokku (EUR) 202.80\n"
                "Maksmisele kuuluv summa 202.80\n"
                "SimplBooks OÜ\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_purchase_invoice_pdf(
                    source,
                    period_start=date(2023, 11, 1),
                    period_end=date(2023, 11, 30),
                    base_currency="EUR",
                )

            self.assertFalse(exceptions)
            self.assertEqual(len(records["purchase_expenses"]), 1)
            expense = records["purchase_expenses"][0]
            self.assertEqual(expense["gross_amount"], 202.8)
            self.assertEqual(expense["net_amount"], 169.0)
            self.assertEqual(expense["vat_amount"], 33.8)
            self.assertEqual(expense["channel"], "simplbooks-ou")

    def test_simplbooks_invoice_uses_supplier_not_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = pdf_source(root, "2024.11.28_Simplbooks.pdf", source_system="document")
            pages = [
                "Arve nr EE24111268\n"
                "Kuupäev 18.11.2024\n"
                "KLIENT\n"
                "Plepic Games OÜ\n"
                "Pihlaka 2\n"
                "Toode/Teenus Hind Kogus Summa KM Kokku\n"
                "SimplBooks raamatupidamistarkvara teenustasu\n"
                "Summa km-ta 22% 169.00\n"
                "KM 22% 37.18\n"
                "Arve kokku (EUR) 206.18\n"
                "Maksmisele kuuluv summa 206.18\n"
                "SimplBooks OÜ\n"
                "Sõpruse pst. 145, 13425 Tallinn\n"
                "Reg-nr: 12213296\n"
            ]
            with mock.patch.object(bookprep, "extract_pdf_pages", return_value=pages):
                records, exceptions = bookprep.parse_purchase_invoice_pdf(
                    source,
                    period_start=date(2024, 11, 1),
                    period_end=date(2024, 11, 30),
                    base_currency="EUR",
                )

            self.assertFalse(exceptions)
            expense = records["purchase_expenses"][0]
            self.assertEqual(expense["attributes"]["vendor_name"], "SimplBooks OÜ")
            self.assertEqual(expense["external_ref"], "EE24111268")

    def test_build_normalized_document_embeds_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "report_2023.csv"
            path.write_text("Date,Orders,Gross sales,Returns,Coupons,Net sales,Taxes,Shipping,Total sales\n", encoding="utf-8")
            period_start, period_end = bookprep.parse_period("2023-01")
            source = bookprep.inspect_source_file(path=path, root_dir=root, period_start=period_start, period_end=period_end)
            assert source is not None
            source.canonical = True
            document = bookprep.build_normalized_document(
                company_slug="example",
                period="2023-01",
                base_currency="EUR",
                sources=[source],
                records=bookprep.parser_result(),
                exceptions=[],
            )

            self.assertEqual(document["company_slug"], "example")
            self.assertEqual(len(document["sources"]), 1)
            self.assertTrue(document["sources"][0]["canonical"])
            json.dumps(document)


if __name__ == "__main__":
    unittest.main()
