"""QQ 邮箱按需收信。

PTrade 策略把盘中筛选结果发到 QQ 邮箱，这里只在用户显式触发时用 IMAP
只读拉取一次——没有守护进程、没有定时轮询，应用闲置时不会碰邮箱。

正文格式与 parser.py 认的完全一致（selected_head / near_head 区段、
"邮件发送时间" 页脚），所以取到正文直接交给 parse_ptrade_report 即可。

只依赖标准库：imaplib / email / html.parser。
"""

from __future__ import annotations

import hashlib
import html
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser


DEFAULT_IMAP_HOST = "imap.qq.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_MAILBOX = "INBOX"
DEFAULT_SINCE_DAYS = 3
DEFAULT_FETCH_LIMIT = 80
DEFAULT_MAX_BODY_CHARS = 200_000
DEFAULT_SUBJECT_KEYWORDS = ("PTrade", "选股", "筛选", "dual_side")
# 攒不齐的分片不能无限堆积：上游 relay 没有过期机制，服务器上已经积了
# 一百多条永远等不到兄弟片的孤儿。超过这个天数就在同步时清掉并报诊断。
DEFAULT_SEGMENT_TTL_DAYS = 3

# 上游按字符数切分长邮件，在主题或正文开头标上 (1/3) [2/3] （3/3）。
SEGMENT_RE = re.compile(r"[\[\(（]\s*(\d{1,3})\s*/\s*(\d{1,3})\s*[\]\)）]")
# 同一份报告的不同分片要归到一组：优先用报告自带的轮次/编号，
# 没有就退回到按 5 分钟取整的收信时间。
REPORT_MARKER_RES = (
    re.compile(
        r"(?:运行轮次|run_slot|逐笔截止|cutoff_hhmmss|报告编号|report_id)\s*[:=：]\s*([A-Za-z0-9_.:-]+)",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)((?:1[34])\d{2}(?:\d{2})?)(?!\d)"),
)
# 判定"这封是不是 PTrade 报告"最可靠的一条：正文里有解析器认的区段标记。
# 主题关键词只是预筛，标题被改过也不该漏掉真报告。
SECTION_MARKERS = ("selected_head", "near_head")


class MailboxError(RuntimeError):
    """收信失败。消息面向用户，不带凭据。"""


@dataclass(frozen=True)
class SegmentInfo:
    part_no: int
    total_parts: int
    group_key: str
    clean_subject: str


@dataclass(frozen=True)
class ParsedMail:
    uid: str
    message_id: str
    subject: str
    from_text: str
    to_text: str
    received_at: str
    body: str
    body_hash: str
    segment: SegmentInfo | None

    @property
    def source_key(self) -> str:
        """去重键：Message-ID 最稳，退回 UID，再退回正文哈希。"""
        if self.message_id:
            return "message-id:" + self.message_id
        if self.uid:
            return "uid:" + self.uid
        return "body:" + self.body_hash


def normalize_newlines(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"br", "p", "div", "tr", "li"}:
            self.parts.append("\n")

    def get_text(self) -> str:
        return normalize_newlines(html.unescape(" ".join(self.parts)))


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def raw_header(message: EmailMessage | Message, name: str) -> str:
    """绕开 headerregistry 的严格解析读原始头。

    现实中的邮件常带畸形 Message-ID，Python 3.10 的 email 包在
    Message.get() 里会因此抛 IndexError，整封邮件就废了。
    raw_items() 保留原值，让收信继续走下去。服务器正是 3.10。
    """
    wanted = name.lower()
    try:
        for key, value in message.raw_items():
            if key.lower() == wanted:
                return str(value or "")
    except Exception:
        return ""
    return ""


def decode_address_header(value: str | None) -> str:
    decoded = decode_mime_header(value)
    addresses = getaddresses([decoded])
    if not addresses:
        return decoded
    rendered = []
    for name, addr in addresses:
        if name and addr:
            rendered.append(f"{name} <{addr}>")
        else:
            rendered.append(addr or name)
    return ", ".join(part for part in rendered if part)


def extract_email_addresses(value: str) -> set[str]:
    return {addr.strip().lower() for _name, addr in getaddresses([value or ""]) if addr.strip()}


def _decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    # QQ 邮件的中文正文可能是 utf-8，也可能是 gb18030/gbk；声明的 charset
    # 还常常不准。逐个严格试解，全失败才降级为替换字符。
    charsets = [part.get_content_charset(), "utf-8", "gb18030", "gbk", "latin1"]
    for charset in [item for item in charsets if item]:
        try:
            return payload.decode(charset, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def extract_body(message: EmailMessage | Message) -> str:
    """取正文纯文本。附件一律跳过，text/plain 优先，没有才退回 HTML。"""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            if "attachment" in (part.get("Content-Disposition") or "").lower():
                continue
            text = _decode_payload(part)
            if not text.strip():
                continue
            if part.get_content_type() == "text/plain":
                plain_parts.append(text)
            elif part.get_content_type() == "text/html":
                html_parts.append(text)
    else:
        text = _decode_payload(message)
        if text.strip():
            if message.get_content_type() == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    if plain_parts:
        return normalize_newlines("\n".join(plain_parts))
    if html_parts:
        parser = _HTMLTextExtractor()
        parser.feed("\n".join(html_parts))
        return parser.get_text()
    return ""


def parse_date(value: str | None) -> str:
    """把 Date 头转成 ISO 字符串；缺失或畸形时退回当前时间。"""
    date_text = decode_mime_header(value)
    if not date_text:
        return datetime.now().isoformat(timespec="seconds")
    try:
        return parsedate_to_datetime(date_text).isoformat(timespec="seconds")
    except Exception:
        return datetime.now().isoformat(timespec="seconds")


def _report_marker(clean_subject: str, received_at: str) -> str:
    """从主题里取轮次标记，用来区分同一天的不同轮次。

    只看主题，不看正文：上游把轮次放在主题里（"PTrade盘中筛选结果 1430"），
    而正文的轮次字段只出现在第一片，后续分片没有。若从正文取，同一份
    报告的各分片会算出不同的分组键，永远拼不起来——服务器上那批常年
    攒不齐的分片正是这么来的。
    """
    for pattern in REPORT_MARKER_RES:
        match = pattern.search(clean_subject)
        if match:
            return match.group(1).lower()
    try:
        moment = datetime.fromisoformat(received_at)
        return moment.replace(
            minute=moment.minute - (moment.minute % 5), second=0, microsecond=0
        ).isoformat()
    except Exception:
        return received_at[:16]


def detect_segment(subject: str, body: str, from_text: str, received_at: str) -> SegmentInfo | None:
    match = None
    for text in (subject, body[:400]):
        match = SEGMENT_RE.search(text or "")
        if match:
            break
    if not match:
        return None
    part_no = int(match.group(1))
    total_parts = int(match.group(2))
    if total_parts <= 1 or part_no < 1 or part_no > total_parts:
        return None
    clean_subject = normalize_newlines(SEGMENT_RE.sub("", subject)).strip(" -_")
    sender = ",".join(sorted(extract_email_addresses(from_text))) or from_text.strip().lower()
    group_key = sha256_text(
        "|".join(
            [
                sender,
                clean_subject,
                received_at[:10] if received_at else "",
                _report_marker(clean_subject, received_at),
                str(total_parts),
            ]
        )
    )
    return SegmentInfo(
        part_no=part_no,
        total_parts=total_parts,
        group_key=group_key,
        clean_subject=clean_subject,
    )


def parse_mail_bytes(raw: bytes, uid: str = "", *, max_body_chars: int = DEFAULT_MAX_BODY_CHARS) -> ParsedMail:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    subject = decode_mime_header(raw_header(message, "Subject"))
    from_text = decode_address_header(raw_header(message, "From"))
    to_text = decode_address_header(raw_header(message, "To"))
    received_at = parse_date(raw_header(message, "Date"))
    message_id = decode_mime_header(raw_header(message, "Message-ID")).strip()
    body = extract_body(message)
    if max_body_chars > 0 and len(body) > max_body_chars:
        raise ValueError(f"邮件正文超过 {max_body_chars} 字符上限")
    return ParsedMail(
        uid=uid,
        message_id=message_id,
        subject=subject,
        from_text=from_text,
        to_text=to_text,
        received_at=received_at,
        body=body,
        body_hash=sha256_text(body),
        segment=detect_segment(subject, body, from_text, received_at),
    )


def run_slot_from_subject(subject: str) -> str:
    """从主题里取运行轮次，例如 "PTrade盘中筛选结果 1430" → "1430"。

    上游把轮次放在主题而不是正文，解析器只认正文，所以要在这里补回来。
    """
    for pattern in REPORT_MARKER_RES:
        match = pattern.search(subject or "")
        if match:
            return match.group(1)
    return ""


def looks_like_report(body: str) -> bool:
    lowered = (body or "").lower()
    return any(marker in lowered for marker in SECTION_MARKERS)


def matches_filter(
    mail: ParsedMail,
    subject_keywords: tuple[str, ...] = DEFAULT_SUBJECT_KEYWORDS,
    from_allowlist: tuple[str, ...] = (),
) -> bool:
    """预筛。分片邮件的后续片可能不含区段标记，所以这里只看主题与发件人；
    "是不是真报告" 由 looks_like_report 在拼装完成后判定。"""
    haystack = (mail.subject + "\n" + mail.body[:800]).lower()
    if subject_keywords and not any(keyword.lower() in haystack for keyword in subject_keywords):
        return False
    if from_allowlist:
        allowed = {addr for item in from_allowlist for addr in extract_email_addresses(item)}
        if not (extract_email_addresses(mail.from_text) & allowed):
            return False
    return True


class MailboxClient:
    """IMAP 只读拉取。每次调用开一条连接、用完即关。

    只读打开（readonly=True）意味着不会改动邮箱状态、不标已读，
    与服务器上常驻轮询的 relay 并存不会互相干扰。
    """

    def __init__(
        self,
        address: str,
        auth_code: str,
        *,
        host: str = DEFAULT_IMAP_HOST,
        port: int = DEFAULT_IMAP_PORT,
        mailbox: str = DEFAULT_MAILBOX,
        timeout: float = 30.0,
    ):
        if not address or not auth_code:
            raise MailboxError("请先配置收信邮箱地址与 IMAP 授权码")
        self.address = address
        self.auth_code = auth_code
        self.host = host
        self.port = port
        self.mailbox = mailbox
        self.timeout = timeout

    def _connect(self) -> imaplib.IMAP4_SSL:
        try:
            client = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
        except OSError as exc:
            raise MailboxError(f"无法连接邮件服务器 {self.host}:{self.port}：{exc}") from exc
        try:
            client.login(self.address, self.auth_code)
        except imaplib.IMAP4.error as exc:
            self._close(client)
            # 不要把授权码带进异常文本。
            raise MailboxError("邮箱登录失败，请检查邮箱地址与 IMAP 授权码是否正确") from exc
        status, _ = client.select(self.mailbox, readonly=True)
        if status != "OK":
            self._close(client)
            raise MailboxError(f"无法打开邮箱文件夹 {self.mailbox}")
        return client

    @staticmethod
    def _close(client: imaplib.IMAP4_SSL) -> None:
        try:
            client.logout()
        except Exception:
            pass

    def test_connection(self) -> dict[str, str]:
        """只登录并打开收件箱，不拉任何邮件。

        这一步就能把"授权码不对"和"能连上但还没有报告"分开，
        用户在设置里点一下就知道问题出在哪一层。
        """
        self._close(self._connect())
        return {
            "message": f"邮箱连接成功：{self.address} · {self.host}/{self.mailbox}",
            "mailbox": self.mailbox,
        }

    def fetch(
        self,
        *,
        last_uid: str = "",
        since_days: int = DEFAULT_SINCE_DAYS,
        limit: int = DEFAULT_FETCH_LIMIT,
        max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
    ) -> tuple[list[ParsedMail], str, list[str]]:
        """拉一批邮件。

        返回 (邮件列表, 见过的最大 UID, 逐封的错误描述)。
        单封解析失败不中断整批——记进 errors 继续下一封。
        """
        client = self._connect()
        errors: list[str] = []
        try:
            uids = self._search(client, last_uid=last_uid, since_days=since_days)
            # `UID SEARCH UID n:*` 在没有更新邮件时仍会返回最大的那个 UID，
            # 所以必须再按数值过滤一次，否则每次同步都会重复处理最后一封。
            if last_uid.isdigit():
                uids = [uid for uid in uids if uid.isdigit() and int(uid) > int(last_uid)]
            uids = uids[-max(1, limit):]
            highest = last_uid
            mails: list[ParsedMail] = []
            for uid in uids:
                raw = self._fetch_one(client, uid, errors)
                if raw is None:
                    continue
                try:
                    mails.append(parse_mail_bytes(raw, uid=uid, max_body_chars=max_body_chars))
                except Exception as exc:
                    errors.append(f"邮件 {uid} 解析失败：{exc}")
                    continue
                if uid.isdigit() and (not highest.isdigit() or int(uid) > int(highest)):
                    highest = uid
            return mails, highest, errors
        finally:
            self._close(client)

    def _search(self, client: imaplib.IMAP4_SSL, *, last_uid: str, since_days: int) -> list[str]:
        # 有游标就走 UID 增量，交互式同步只取真正的新邮件；
        # 首次同步或游标失效时退回按日期回溯。
        if last_uid.isdigit():
            criteria = ("UID", f"{int(last_uid) + 1}:*")
        else:
            since = (datetime.now() - timedelta(days=max(1, since_days))).strftime("%d-%b-%Y")
            criteria = ("SINCE", since)
        try:
            status, data = client.uid("search", None, *criteria)
        except imaplib.IMAP4.error as exc:
            raise MailboxError(f"邮箱检索失败：{exc}") from exc
        if status != "OK":
            raise MailboxError("邮箱检索失败")
        if not data or not data[0]:
            return []
        return [item.decode("ascii", errors="replace") for item in data[0].split()]

    def _fetch_one(self, client: imaplib.IMAP4_SSL, uid: str, errors: list[str]) -> bytes | None:
        try:
            status, data = client.uid("fetch", uid, "(RFC822)")
        except imaplib.IMAP4.error as exc:
            errors.append(f"邮件 {uid} 拉取失败：{exc}")
            return None
        if status != "OK":
            errors.append(f"邮件 {uid} 拉取失败")
            return None
        for item in data or []:
            if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        errors.append(f"邮件 {uid} 内容为空")
        return None
