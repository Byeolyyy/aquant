"""把邮箱里的 PTrade 邮件收成可分析的报告。

编排层：串起 mailbox（取信与解析）、parser（确定性解析）与 repository（落库）。
只在用户显式触发同步时运行一次，不常驻。

去重是两层的：
- source_key（Message-ID 优先）挡住重复拉到的同一封邮件；
- content_hash 挡住内容相同的报告，跨来源生效——手动粘贴过的报告
  之后又由邮件送达时，复用同一条记录而不是新增一份。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .mailbox import (
    DEFAULT_FETCH_LIMIT,
    DEFAULT_MAX_BODY_CHARS,
    DEFAULT_SEGMENT_TTL_DAYS,
    DEFAULT_SINCE_DAYS,
    DEFAULT_SUBJECT_KEYWORDS,
    MailboxClient,
    ParsedMail,
    looks_like_report,
    matches_filter,
    run_slot_from_subject,
)
from .models import ParsedReport
from .parser import parse_ptrade_report
from .repository import Repository


MAIL_LAST_UID = "mail.last_uid"


@dataclass
class SyncResult:
    fetched: int = 0
    new_reports: int = 0
    duplicate_reports: int = 0
    skipped: int = 0
    stored_segments: int = 0
    assembled_groups: int = 0
    expired_groups: int = 0
    pending_groups: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_payload(self) -> dict:
        return {
            "fetched": self.fetched,
            "new_reports": self.new_reports,
            "duplicate_reports": self.duplicate_reports,
            "skipped": self.skipped,
            "stored_segments": self.stored_segments,
            "assembled_groups": self.assembled_groups,
            "expired_groups": self.expired_groups,
            "pending_groups": self.pending_groups,
            "errors": self.errors,
        }


class MailIngestor:
    def __init__(
        self,
        repository: Repository,
        client: MailboxClient,
        *,
        subject_keywords: tuple[str, ...] = DEFAULT_SUBJECT_KEYWORDS,
        from_allowlist: tuple[str, ...] = (),
        segment_ttl_days: int = DEFAULT_SEGMENT_TTL_DAYS,
    ):
        self.repository = repository
        self.client = client
        self.subject_keywords = subject_keywords
        self.from_allowlist = from_allowlist
        self.segment_ttl_days = segment_ttl_days

    def sync(
        self,
        *,
        since_days: int = DEFAULT_SINCE_DAYS,
        limit: int = DEFAULT_FETCH_LIMIT,
        max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
    ) -> SyncResult:
        result = SyncResult()

        cutoff = (datetime.now() - timedelta(days=max(1, self.segment_ttl_days))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        result.expired_groups = len(self.repository.expire_mail_segments(cutoff))

        last_uid = self.repository.get_setting(MAIL_LAST_UID)
        mails, highest_uid, fetch_errors = self.client.fetch(
            last_uid=last_uid,
            since_days=since_days,
            limit=limit,
            max_body_chars=max_body_chars,
        )
        result.errors.extend(fetch_errors)
        result.fetched = len(mails)

        for mail in mails:
            try:
                self._ingest_one(mail, result)
            except Exception as exc:
                result.errors.append(f"邮件「{mail.subject or mail.uid}」处理失败：{exc}")

        self._assemble_ready_segments(result)

        # 游标只在整批处理完之后推进：中途异常时下次同步会重拉，
        # 靠 source_key 去重挡住重复，不会丢邮件。
        if highest_uid and highest_uid != last_uid:
            self.repository.set_settings({MAIL_LAST_UID: highest_uid})

        result.pending_groups = self.repository.pending_mail_segments()
        return result

    def _ingest_one(self, mail: ParsedMail, result: SyncResult) -> None:
        if self.repository.mail_message_seen(mail.source_key):
            result.skipped += 1
            return
        if not matches_filter(mail, self.subject_keywords, self.from_allowlist):
            # 记为已见，避免每次同步都重新判定同一批无关邮件。
            self.repository.record_mail_message(
                mail.source_key,
                uid=mail.uid,
                message_id=mail.message_id,
                subject=mail.subject,
                received_at=mail.received_at,
                body_hash=mail.body_hash,
            )
            result.skipped += 1
            return

        if mail.segment is not None:
            self.repository.save_mail_segment(
                mail.segment.group_key,
                mail.segment.part_no,
                mail.segment.total_parts,
                subject=mail.segment.clean_subject or mail.subject,
                body=mail.body,
                received_at=mail.received_at,
            )
            self.repository.record_mail_message(
                mail.source_key,
                uid=mail.uid,
                message_id=mail.message_id,
                subject=mail.subject,
                received_at=mail.received_at,
                body_hash=mail.body_hash,
            )
            result.stored_segments += 1
            return

        if not looks_like_report(mail.body):
            self.repository.record_mail_message(
                mail.source_key,
                uid=mail.uid,
                message_id=mail.message_id,
                subject=mail.subject,
                received_at=mail.received_at,
                body_hash=mail.body_hash,
            )
            result.skipped += 1
            return

        report_id, is_new = self._store_report(mail.body, mail.subject, mail.received_at)
        self.repository.record_mail_message(
            mail.source_key,
            uid=mail.uid,
            message_id=mail.message_id,
            subject=mail.subject,
            received_at=mail.received_at,
            body_hash=mail.body_hash,
            report_id=report_id,
        )
        if is_new:
            result.new_reports += 1
        else:
            result.duplicate_reports += 1

    def _assemble_ready_segments(self, result: SyncResult) -> None:
        for group in self.repository.ready_mail_segments():
            body = str(group.get("body") or "")
            if looks_like_report(body):
                _report_id, is_new = self._store_report(
                    body, str(group.get("subject") or ""), str(group.get("received_at") or "")
                )
                if is_new:
                    result.new_reports += 1
                else:
                    result.duplicate_reports += 1
                result.assembled_groups += 1
            else:
                result.errors.append(
                    f"分片邮件「{group.get('subject') or group.get('group_key')}」拼装后仍未找到报告区段，已丢弃"
                )
            self.repository.delete_mail_segments(str(group.get("group_key") or ""))

    def _store_report(self, body: str, subject: str, received_at: str) -> tuple[str, bool]:
        report = parse_ptrade_report(body)
        existing = self.repository.find_report_by_hash(report.content_hash)
        if existing:
            return existing, False
        report = self._tag_source(report, subject, received_at)
        self.repository.save_report(report)
        return report.report_id, True

    @staticmethod
    def _tag_source(report: ParsedReport, subject: str, received_at: str) -> ParsedReport:
        """标注来源，并把邮件才有的元数据补进报告。

        上游正文只打 selected_head / near_head 两个区段：运行轮次在主题里，
        报告时间靠 "邮件发送时间" 页脚而当前发信脚本并不总是带上。两者缺失
        时用邮件自身的主题与 Date 头兜底，否则列表里会是一排没有时间、没有
        轮次的条目，用户根本没法"按时间挑一份"。
        """
        updates: dict[str, str] = {
            "source": "mail",
            "mail_subject": subject,
            "mail_received_at": received_at,
        }
        if not report.run_slot:
            slot = run_slot_from_subject(subject)
            if slot:
                updates["run_slot"] = slot
        if not report.generated_at and received_at:
            updates["generated_at"] = _readable_timestamp(received_at)
            if not report.report_date:
                updates["report_date"] = received_at[:10]
        return report.model_copy(update=updates)


def _readable_timestamp(value: str) -> str:
    """把邮件 Date 的 ISO 形式转成与解析器一致的 "YYYY-MM-DD HH:MM:SS"。"""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value
