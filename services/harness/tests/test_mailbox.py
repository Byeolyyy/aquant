from __future__ import annotations

import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from quant_agent_harness.mail_ingest import MAIL_LAST_UID, MailIngestor
from quant_agent_harness.mailbox import (
    ParsedMail,
    detect_segment,
    extract_body,
    looks_like_report,
    matches_filter,
    parse_mail_bytes,
)
from quant_agent_harness.repository import Repository


REPORT_BODY = """生成时间: 2026-08-20 14:30:00
运行轮次: 1430
selected_head:
symbol reason realtime_formula_wanyuan flow_threshold_wanyuan vol_ratio turnover_now_pct l4_buy_sell
600000.SS all_conditions_met 4300 4000 1.2 2.5 True
near_head: empty
邮件发送时间: 2026-08-20 14:30:05"""


def build_mail(
    body: str,
    *,
    subject: str = "PTrade盘中筛选结果",
    sender: str = "strategy@example.com",
    message_id: str = "<msg-1@example.com>",
    charset: str = "utf-8",
    html_body: str = "",
    attachment: bool = False,
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "inbox@example.com"
    message["Date"] = "Wed, 20 Aug 2026 14:30:05 +0800"
    message["Message-ID"] = message_id
    if html_body:
        message.set_content(html_body, subtype="html", charset=charset)
    else:
        message.set_content(body, charset=charset)
    if attachment:
        message.add_attachment(
            b"symbol,reason\n000001.SZ,noise\n",
            maintype="text",
            subtype="csv",
            filename="noise.csv",
        )
    return message.as_bytes()


class FakeMailboxClient:
    """按 UID 顺序吐出预置邮件，复现 IMAP 的增量语义。"""

    def __init__(self, mails: list[tuple[str, bytes]]):
        self.mails = mails
        self.fetch_calls: list[str] = []

    def fetch(self, *, last_uid="", since_days=3, limit=80, max_body_chars=200_000):
        self.fetch_calls.append(last_uid)
        selected = [
            (uid, raw)
            for uid, raw in self.mails
            if not last_uid.isdigit() or int(uid) > int(last_uid)
        ]
        parsed = [parse_mail_bytes(raw, uid=uid, max_body_chars=max_body_chars) for uid, raw in selected]
        highest = last_uid
        for uid, _raw in selected:
            if not highest.isdigit() or int(uid) > int(highest):
                highest = uid
        return parsed, highest, []


class MailParsingTests(unittest.TestCase):
    def test_plain_body_is_extracted_and_recognised_as_report(self):
        mail = parse_mail_bytes(build_mail(REPORT_BODY), uid="10")
        self.assertIn("selected_head", mail.body)
        self.assertTrue(looks_like_report(mail.body))
        self.assertEqual(mail.message_id, "<msg-1@example.com>")
        self.assertTrue(mail.received_at.startswith("2026-08-20"))
        self.assertEqual(mail.source_key, "message-id:<msg-1@example.com>")

    def test_gb18030_chinese_body_survives_decoding(self):
        mail = parse_mail_bytes(build_mail(REPORT_BODY, charset="gb18030"), uid="11")
        self.assertIn("生成时间", mail.body)
        self.assertIn("运行轮次", mail.body)

    def test_attachment_is_ignored_and_plain_text_wins(self):
        mail = parse_mail_bytes(build_mail(REPORT_BODY, attachment=True), uid="12")
        self.assertIn("selected_head", mail.body)
        self.assertNotIn("noise.csv", mail.body)
        self.assertNotIn("000001.SZ", mail.body)

    def test_html_only_body_falls_back_to_text_extraction(self):
        html = "<div>生成时间: 2026-08-20 14:30:00</div><div>selected_head:</div><div>600000.SS</div>"
        mail = parse_mail_bytes(build_mail("", html_body=html), uid="13")
        self.assertIn("selected_head", mail.body)
        self.assertIn("600000.SS", mail.body)
        self.assertNotIn("<div>", mail.body)

    def test_malformed_message_id_does_not_break_parsing(self):
        # Python 3.10 的 email 包对畸形 Message-ID 会在 get() 里抛 IndexError，
        # raw_items() 这条路要能把整封邮件救回来。
        mail = parse_mail_bytes(build_mail(REPORT_BODY, message_id="not-a-valid-id"), uid="14")
        self.assertIn("selected_head", mail.body)
        self.assertTrue(mail.source_key)

    def test_filter_rejects_unrelated_mail_but_keeps_report(self):
        report = parse_mail_bytes(build_mail(REPORT_BODY), uid="15")
        unrelated = parse_mail_bytes(build_mail("今天午餐吃什么", subject="午餐投票"), uid="16")
        self.assertTrue(matches_filter(report))
        self.assertFalse(matches_filter(unrelated))

    def test_from_allowlist_blocks_other_senders(self):
        mail = parse_mail_bytes(build_mail(REPORT_BODY, sender="stranger@evil.com"), uid="17")
        self.assertTrue(matches_filter(mail, from_allowlist=()))
        self.assertFalse(matches_filter(mail, from_allowlist=("strategy@example.com",)))


class SegmentDetectionTests(unittest.TestCase):
    def test_segment_marker_is_detected_in_subject(self):
        info = detect_segment("PTrade盘中筛选结果 (2/3)", REPORT_BODY, "a@b.com", "2026-08-20T14:30:05")
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual((info.part_no, info.total_parts), (2, 3))
        self.assertNotIn("(2/3)", info.clean_subject)

    def test_parts_of_same_report_share_a_group_key(self):
        first = detect_segment("PTrade结果 (1/2)", REPORT_BODY, "a@b.com", "2026-08-20T14:30:05")
        second = detect_segment("PTrade结果 (2/2)", REPORT_BODY, "a@b.com", "2026-08-20T14:30:09")
        assert first is not None and second is not None
        self.assertEqual(first.group_key, second.group_key)

    def test_single_part_marker_is_not_a_segment(self):
        self.assertIsNone(detect_segment("PTrade结果 (1/1)", REPORT_BODY, "a@b.com", "2026-08-20T14:30:05"))

    def test_full_width_brackets_are_supported(self):
        info = detect_segment("PTrade结果（3/4）", REPORT_BODY, "a@b.com", "2026-08-20T14:30:05")
        assert info is not None
        self.assertEqual((info.part_no, info.total_parts), (3, 4))


class MailIngestTests(unittest.TestCase):
    def _ingestor(self, temp_dir: str, mails: list[tuple[str, bytes]]):
        repository = Repository(Path(temp_dir) / "test.sqlite")
        client = FakeMailboxClient(mails)
        return repository, client, MailIngestor(repository, client)  # type: ignore[arg-type]

    def test_report_mail_becomes_a_listed_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, _client, ingestor = self._ingestor(temp_dir, [("10", build_mail(REPORT_BODY))])
            result = ingestor.sync()
            self.assertEqual(result.new_reports, 1)
            reports = repository.list_reports()
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["source"], "mail")
            self.assertEqual(reports[0]["run_slot"], "1430")
            self.assertEqual(reports[0]["selected_count"], 1)
            self.assertEqual(reports[0]["parse_status"], "valid")
            self.assertIn("PTrade", reports[0]["mail_subject"])

    def test_second_sync_does_not_duplicate_the_same_mail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, _client, ingestor = self._ingestor(temp_dir, [("10", build_mail(REPORT_BODY))])
            ingestor.sync()
            second = ingestor.sync()
            self.assertEqual(second.fetched, 0)
            self.assertEqual(second.new_reports, 0)
            self.assertEqual(len(repository.list_reports()), 1)

    def test_uid_cursor_advances_so_old_mail_is_not_refetched(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, client, ingestor = self._ingestor(temp_dir, [("10", build_mail(REPORT_BODY))])
            ingestor.sync()
            self.assertEqual(repository.get_setting(MAIL_LAST_UID), "10")
            ingestor.sync()
            self.assertEqual(client.fetch_calls, ["", "10"])

    def test_same_content_from_a_different_mail_reuses_one_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, _client, ingestor = self._ingestor(
                temp_dir,
                [
                    ("10", build_mail(REPORT_BODY, message_id="<a@x.com>")),
                    ("11", build_mail(REPORT_BODY, message_id="<b@x.com>")),
                ],
            )
            result = ingestor.sync()
            self.assertEqual(result.new_reports, 1)
            self.assertEqual(result.duplicate_reports, 1)
            self.assertEqual(len(repository.list_reports()), 1)

    def test_unrelated_mail_is_skipped_without_creating_a_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, _client, ingestor = self._ingestor(
                temp_dir, [("10", build_mail("今天午餐吃什么", subject="午餐投票"))]
            )
            result = ingestor.sync()
            self.assertEqual(result.skipped, 1)
            self.assertEqual(repository.list_reports(), [])

    def test_matching_subject_without_report_sections_is_not_ingested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository, _client, ingestor = self._ingestor(
                temp_dir, [("10", build_mail("今日 PTrade 策略无信号", subject="PTrade盘中筛选结果"))]
            )
            result = ingestor.sync()
            self.assertEqual(result.new_reports, 0)
            self.assertEqual(result.skipped, 1)
            self.assertEqual(repository.list_reports(), [])


SLIM_BODY = """selected_head:
symbol super_net_wanyuan large_net_wanyuan medium_net_wanyuan small_net_wanyuan realtime_formula_wanyuan realtime_formula_ratio_pct l4_buy_sell vol_ratio turnover_now_pct
688368.SS 8564.53 3000.0 -1200.0 -1500.0 9421.47 0.33 True 1.42 1.93
near_head: empty"""


class UpstreamSlimFormatTests(unittest.TestCase):
    """上游实际发出的精简格式：正文只有两个区段，轮次在主题里。"""

    def _sync(self, temp_dir: str, body: str, subject: str):
        repository = Repository(Path(temp_dir) / "test.sqlite")
        client = FakeMailboxClient([("10", build_mail(body, subject=subject))])
        MailIngestor(repository, client).sync()  # type: ignore[arg-type]
        return repository.list_reports()

    def test_slim_body_parses_with_injected_threshold_and_reason(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reports = self._sync(temp_dir, SLIM_BODY, "PTrade盘中筛选结果 1430")
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["parse_status"], "valid")
            self.assertEqual(reports[0]["selected_count"], 1)

    def test_run_slot_falls_back_to_the_subject(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reports = self._sync(temp_dir, SLIM_BODY, "PTrade盘中筛选结果 1430")
            self.assertEqual(reports[0]["run_slot"], "1430")

    def test_report_time_falls_back_to_the_mail_date_header(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reports = self._sync(temp_dir, SLIM_BODY, "PTrade盘中筛选结果 1430")
            # 正文没有页脚时间，只能用邮件自身的 Date 头兜底，
            # 否则列表里这一条就没有时间可选。
            self.assertEqual(reports[0]["report_date"], "2026-08-20")
            self.assertTrue(reports[0]["generated_at"].startswith("2026-08-20 14:30"))

    def test_footer_timestamp_wins_over_the_mail_date_header(self):
        body = SLIM_BODY + "\n}邮件发送时间:{2026-08-19 14:31:46}"
        with tempfile.TemporaryDirectory() as temp_dir:
            reports = self._sync(temp_dir, body, "PTrade盘中筛选结果 1430")
            self.assertEqual(reports[0]["generated_at"], "2026-08-19 14:31:46")
            self.assertEqual(reports[0]["report_date"], "2026-08-19")


class SegmentAssemblyTests(unittest.TestCase):
    def _split_report(self) -> tuple[str, str]:
        head, _, tail = REPORT_BODY.partition("near_head: empty")
        return head.rstrip(), "near_head: empty" + tail

    def test_incomplete_segments_wait_and_are_reported_as_pending(self):
        first, _second = self._split_report()
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            client = FakeMailboxClient([("10", build_mail(first, subject="PTrade结果 (1/2)"))])
            result = MailIngestor(repository, client).sync()  # type: ignore[arg-type]
            self.assertEqual(result.stored_segments, 1)
            self.assertEqual(result.new_reports, 0)
            self.assertEqual(len(result.pending_groups), 1)
            self.assertEqual(result.pending_groups[0]["received_parts"], 1)
            self.assertEqual(result.pending_groups[0]["total_parts"], 2)
            self.assertEqual(repository.list_reports(), [])

    def test_all_parts_arriving_out_of_order_assemble_into_one_report(self):
        first, second = self._split_report()
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            client = FakeMailboxClient(
                [
                    ("10", build_mail(second, subject="PTrade结果 (2/2)", message_id="<p2@x.com>")),
                    ("11", build_mail(first, subject="PTrade结果 (1/2)", message_id="<p1@x.com>")),
                ]
            )
            result = MailIngestor(repository, client).sync()  # type: ignore[arg-type]
            self.assertEqual(result.assembled_groups, 1)
            self.assertEqual(result.new_reports, 1)
            self.assertEqual(result.pending_groups, [])
            reports = repository.list_reports()
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["parse_status"], "valid")
            self.assertEqual(reports[0]["selected_count"], 1)

    def test_duplicate_part_does_not_fake_completeness(self):
        first, _second = self._split_report()
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            client = FakeMailboxClient(
                [
                    ("10", build_mail(first, subject="PTrade结果 (1/2)", message_id="<p1@x.com>")),
                    ("11", build_mail(first, subject="PTrade结果 (1/2)", message_id="<dup@x.com>")),
                ]
            )
            result = MailIngestor(repository, client).sync()  # type: ignore[arg-type]
            self.assertEqual(result.assembled_groups, 0)
            self.assertEqual(len(result.pending_groups), 1)
            self.assertEqual(result.pending_groups[0]["received_parts"], 1)

    def test_stale_incomplete_segments_are_expired(self):
        first, _second = self._split_report()
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = Repository(Path(temp_dir) / "test.sqlite")
            client = FakeMailboxClient([("10", build_mail(first, subject="PTrade结果 (1/2)"))])
            MailIngestor(repository, client).sync()  # type: ignore[arg-type]
            self.assertEqual(len(repository.pending_mail_segments()), 1)

            expired = repository.expire_mail_segments("2999-01-01 00:00:00")
            self.assertEqual(len(expired), 1)
            self.assertEqual(repository.pending_mail_segments(), [])


if __name__ == "__main__":
    unittest.main()
