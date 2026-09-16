from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from hashlib import sha256
import imaplib
import re
import socket
import ssl
from time import monotonic
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .models import EmailMessage, MailCursor, MailIdentity
from .sanitization import redact_sensitive_text


class MailConnectorError(RuntimeError):
    pass


DEFAULT_IMAP_TIMEOUT_SECONDS = 30.0


class ImapConnectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=993, ge=1, le=65_535)
    username: str = Field(min_length=1, max_length=512)
    password: SecretStr
    mailbox: str = Field(default="INBOX", min_length=1, max_length=256)
    trusted_authserv_ids: list[str] = Field(default_factory=list, max_length=16)
    timeout_seconds: float = Field(
        default=DEFAULT_IMAP_TIMEOUT_SECONDS,
        gt=0,
        le=300,
    )


class MailFetchBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[EmailMessage] = Field(default_factory=list)
    next_cursor: MailCursor


class MailConnector(Protocol):
    def fetch_since(self, cursor: MailCursor | None = None, *, limit: int = 100) -> MailFetchBatch: ...


ImapFactory = Callable[[str, int, ssl.SSLContext], imaplib.IMAP4_SSL]

_NETEASE_PERSONAL_IMAP_HOSTS = frozenset(
    {"imap.163.com", "imap.126.com", "imap.yeah.net"}
)
_NETEASE_CLIENT_ID = (
    '("name" "RecruitOps Agent" "version" "0.1.1" '
    '"vendor" "RecruitOps")'
)
_AUTH_RESULTS = frozenset({"pass", "fail", "softfail", "neutral", "none", "temperror", "permerror", "unknown"})
_AUTH_METHODS = frozenset({"spf", "dkim", "dmarc", "arc"})
_AUTH_RESULT_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<method>spf|dkim|dmarc|arc)\s*=\s*"
    r"(?P<result>pass|fail|softfail|neutral|none|temperror|permerror|unknown)"
    r"(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_NETEASE_AUTHSERV_RE = re.compile(
    r"(?:gzchengxin\d+|gzmx\d+|gzga-mx-mtada-g\d+-\d+)\Z",
    re.IGNORECASE,
)
_AUTH_PROPERTY_RE = re.compile(
    r"(?i)\b(?:header\.d|header\.from|header\.i|smtp\.mail|smtp\.mailfrom)\s*=\s*"
    r"(?P<value>[^;,()]*?)(?=\s+[a-z][a-z0-9_-]*\.[a-z][a-z0-9_-]*\s*=|[;,()]|$)"
)
_AUTH_ADDRESS_RE = re.compile(
    r"(?i)@\s*(?P<domain>[A-Za-z0-9.-]+(?:\s+[A-Za-z0-9.-]+)*)"
)
_FLAGS_RE = re.compile(r"\bFLAGS\s*\((?P<flags>[^)]*)\)", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def _default_imap_factory(
    host: str,
    port: int,
    context: ssl.SSLContext,
    *,
    timeout: float = DEFAULT_IMAP_TIMEOUT_SECONDS,
) -> imaplib.IMAP4_SSL:
    return imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=timeout)


def _identify_netease_client(client: imaplib.IMAP4_SSL, host: str) -> None:
    if host.casefold().rstrip(".") not in _NETEASE_PERSONAL_IMAP_HOSTS:
        return
    # NetEase personal mail requires RFC 2971 ID before SELECT/EXAMINE.
    imaplib.Commands.setdefault("ID", ("AUTH",))
    status, _ = client._simple_command("ID", _NETEASE_CLIENT_ID)
    if status != "OK":
        raise MailConnectorError("imap_client_identity_rejected")


def _decode_part(part: Message, *, maximum: int) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    payload = payload[:maximum]
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _body(message: Message, *, maximum: int) -> tuple[str, str | None]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    remaining = maximum
    for part in parts:
        if remaining <= 0 or part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").casefold()
        if disposition == "attachment":
            continue
        content_type = part.get_content_type().casefold()
        if content_type not in {"text/plain", "text/html"}:
            continue
        value = _decode_part(part, maximum=remaining)
        remaining -= len(value.encode("utf-8", errors="ignore"))
        (html_parts if content_type == "text/html" else text_parts).append(value)
    return "\n".join(text_parts), "\n".join(html_parts) or None


def _raw_message(fetch_data: object) -> bytes | None:
    if not isinstance(fetch_data, (list, tuple)):
        return None
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def _mailbox_read_from_fetch(fetch_data: object) -> bool | None:
    if not isinstance(fetch_data, (list, tuple)):
        return None
    for item in fetch_data:
        if not isinstance(item, tuple) or not item:
            continue
        descriptor = item[0]
        if not isinstance(descriptor, bytes):
            continue
        match = _FLAGS_RE.search(descriptor.decode("ascii", errors="ignore"))
        if match is None:
            continue
        flags = {value.casefold() for value in match.group("flags").split()}
        return "\\seen" in flags
    return None


def _normalise_domain_name(value: str | None) -> str | None:
    if not value:
        return None
    domain = value.strip().strip("<>").rstrip(".")
    if not domain:
        return None
    try:
        domain = domain.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return None
    return domain if _DOMAIN_RE.fullmatch(domain) else None


def _normalise_domain(value: str | None) -> str | None:
    if not value or "@" not in value:
        return None
    return _normalise_domain_name(value.rsplit("@", 1)[1])


def _address_domain(value: str | None) -> str | None:
    if not value:
        return None
    _display_name, address = __import__("email.utils", fromlist=["parseaddr"]).parseaddr(value)
    return _normalise_domain(address)


def _header_value(message: Message, name: str) -> str | None:
    value = message.get(name)
    if value is None:
        return None
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())


def _header_message_id(message: Message, uid: str) -> tuple[str, str]:
    raw_message_id = _header_value(message, "Message-ID")
    if raw_message_id:
        return raw_message_id.strip(), "header"
    return f"imap:{uid}", "imap_uid_fallback"


def _authserv_matches(authserv_id: str, trusted_ids: set[str]) -> bool:
    if authserv_id in trusted_ids:
        return True
    if any(authserv_id.endswith(f".{trusted_id}") for trusted_id in trusted_ids if "." in trusted_id):
        return True
    return bool(
        _NETEASE_AUTHSERV_RE.fullmatch(authserv_id)
        and trusted_ids.intersection({"163.com", "126.com", "yeah.net", "coremail", "coremail.net"})
    )


def _domains_align(authenticated_domain: str | None, sender_domain: str | None) -> bool:
    if not authenticated_domain or not sender_domain:
        return False
    left = authenticated_domain.casefold().rstrip(".")
    right = sender_domain.casefold().rstrip(".")
    if "." not in left or "." not in right:
        return False
    return left == right or left.endswith(f".{right}") or right.endswith(f".{left}")


def _authentication_identity_domain(segment: str) -> str | None:
    property_match = _AUTH_PROPERTY_RE.search(segment)
    if property_match is not None:
        value = "".join(property_match.group("value").split()).strip("<>")
        domain = _normalise_domain(value) if "@" in value else _normalise_domain_name(value)
        return domain if domain and "." in domain else None
    address_match = _AUTH_ADDRESS_RE.search(segment)
    if address_match is not None:
        domain = "".join(address_match.group("domain").split())
        return _normalise_domain_name(domain)
    return None


def _authentication_results(
    message: Message,
    trusted_ids: set[str],
    *,
    sender_domain: str | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    raw_headers = message.get_all("Authentication-Results", [])
    for raw_header in raw_headers[:1]:
        header = " ".join(str(raw_header).replace("\r", " ").replace("\n", " ").split())
        authserv_id = header.split(";", 1)[0].strip().split(" ", 1)[0].casefold()
        if not authserv_id or not _authserv_matches(authserv_id, trusted_ids):
            continue
        matches = list(_AUTH_RESULT_RE.finditer(header))
        for index, match in enumerate(matches):
            method = match.group("method").casefold()
            result = match.group("result").casefold()
            if method not in _AUTH_METHODS or result not in _AUTH_RESULTS:
                continue
            key = (authserv_id, method, result)
            if key in seen:
                continue
            seen.add(key)
            end = matches[index + 1].start() if index + 1 < len(matches) else len(header)
            identity_domain = _authentication_identity_domain(header[match.end():end])
            item: dict[str, Any] = {
                "method": method,
                "result": result,
                "authserv_id": authserv_id,
                "aligned": _domains_align(identity_domain, sender_domain),
            }
            if identity_domain is not None:
                item["identity_domain"] = identity_domain
            results.append(item)
    return results


def _source_metadata(
    message: Message,
    *,
    uid: str,
    uid_validity: str | None,
    trusted_authserv_ids: set[str],
    mailbox_read: bool | None = None,
) -> dict[str, Any]:
    message_id, message_id_source = _header_message_id(message, uid)
    sender_domain = _address_domain(_header_value(message, "From"))
    metadata: dict[str, Any] = {
        "message_id": redact_sensitive_text(message_id),
        "message_id_hash": sha256(message_id.encode("utf-8")).hexdigest(),
        "sender_domain": sender_domain,
        "return_path_domain": _address_domain(_header_value(message, "Return-Path")),
        "authentication_results": _authentication_results(
            message,
            trusted_authserv_ids,
            sender_domain=sender_domain,
        ),
        "message_id_source": message_id_source,
        "imap_uid": uid,
        "uid_validity": uid_validity,
    }
    if mailbox_read is not None:
        metadata["mailbox_read"] = mailbox_read
    return {key: value for key, value in metadata.items() if value is not None}


def _response_text(value: object) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        for item in value:
            result = _response_text(item)
            if result:
                return result
    return None


def _uid_validity_from_response(client: object, select_data: object) -> str | None:
    if isinstance(select_data, Mapping):
        for key in ("UIDVALIDITY", b"UIDVALIDITY", "uidvalidity", b"uidvalidity"):
            if key in select_data:
                result = _response_text(select_data[key])
                if result:
                    return result
    response = getattr(client, "response", None)
    if callable(response):
        try:
            _key, data = response("UIDVALIDITY")
        except (AttributeError, TypeError, ValueError, imaplib.IMAP4.error):
            data = None
        result = _response_text(data)
        if result:
            return result
    untagged = getattr(client, "untagged_responses", None)
    if isinstance(untagged, Mapping):
        for key in ("UIDVALIDITY", b"UIDVALIDITY", "uidvalidity", b"uidvalidity"):
            if key in untagged:
                result = _response_text(untagged[key])
                if result:
                    return result
    return None


class ImapReadOnlyConnector:
    """Incremental IMAP reader using read-only mailbox selection and BODY.PEEK."""

    def __init__(
        self,
        config: ImapConnectionConfig,
        *,
        client_factory: ImapFactory = _default_imap_factory,
        maximum_message_bytes: int = 1_000_000,
    ) -> None:
        self.config = config
        self.client_factory = client_factory
        self.maximum_message_bytes = maximum_message_bytes

    def _open_client(
        self,
        context: ssl.SSLContext,
        *,
        timeout_seconds: float | None = None,
    ) -> imaplib.IMAP4_SSL:
        if self.client_factory is _default_imap_factory:
            return _default_imap_factory(
                self.config.host,
                self.config.port,
                context,
                timeout=(
                    self.config.timeout_seconds
                    if timeout_seconds is None
                    else timeout_seconds
                ),
            )
        return self.client_factory(self.config.host, self.config.port, context)

    @staticmethod
    def _apply_deadline(client: object, deadline: float) -> None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("imap sync exceeded its total timeout")
        sock = getattr(client, "sock", None)
        settimeout = getattr(sock, "settimeout", None)
        if callable(settimeout):
            settimeout(remaining)

    @staticmethod
    def _abort_client(client: object) -> None:
        """Close an expired connection without sending another IMAP command."""

        sock = getattr(client, "sock", None)
        if sock is None:
            return
        shutdown = getattr(sock, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        close = getattr(sock, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _cleanup_client(self, client: object, *, deadline: float | None = None) -> None:
        if deadline is None:
            try:
                client.close()
            except Exception:
                pass
            try:
                client.logout()
            except Exception:
                pass
            return

        try:
            self._apply_deadline(client, deadline)
        except TimeoutError:
            self._abort_client(client)
            return
        try:
            client.close()
        except Exception:
            if monotonic() >= deadline:
                self._abort_client(client)
                return

        try:
            self._apply_deadline(client, deadline)
        except TimeoutError:
            self._abort_client(client)
            return
        try:
            client.logout()
        except Exception:
            if monotonic() >= deadline:
                self._abort_client(client)

    def fetch_since(self, cursor: MailCursor | None = None, *, limit: int = 100) -> MailFetchBatch:
        if limit < 1 or limit > 500:
            raise ValueError("mail fetch limit must be between 1 and 500")
        if cursor is not None and cursor.mailbox != self.config.mailbox:
            raise ValueError("mail cursor mailbox does not match connector mailbox")
        # Validate the persisted cursor before opening a network connection.
        # This keeps corrupt local state deterministic and avoids a needless login.
        self._start_uid(cursor)
        client = None
        deadline = monotonic() + self.config.timeout_seconds
        try:
            context = ssl.create_default_context()
            client = self._open_client(
                context,
                timeout_seconds=max(0.001, deadline - monotonic()),
            )
            self._apply_deadline(client, deadline)
            client.login(self.config.username, self.config.password.get_secret_value())
            self._apply_deadline(client, deadline)
            _identify_netease_client(client, self.config.host)
            self._apply_deadline(client, deadline)
            status, select_data = client.select(self.config.mailbox, readonly=True)
            if status != "OK":
                raise MailConnectorError("imap_mailbox_unavailable")
            self._apply_deadline(client, deadline)
            uid_validity = _uid_validity_from_response(client, select_data)
            generation_changed = bool(
                cursor is not None
                and cursor.uid_validity is not None
                and uid_validity is not None
                and cursor.uid_validity != uid_validity
            )
            start_uid = self._start_uid(cursor, uid_validity)
            self._apply_deadline(client, deadline)
            status, search_data = client.uid("search", None, f"UID {start_uid}:*")
            if status != "OK":
                raise MailConnectorError("imap_search_failed")
            raw_uids = search_data[0].split() if search_data and search_data[0] else []
            uids = [
                uid
                for uid in raw_uids
                if uid.isdigit() and int(uid) >= start_uid
            ][:limit]
            messages = [
                self._fetch_message(
                    client,
                    uid,
                    uid_validity=uid_validity,
                    deadline=deadline,
                )
                for uid in uids
            ]
            messages = [item for item in messages if item is not None]
            self._apply_deadline(client, deadline)
            next_token = (
                uids[-1].decode("ascii")
                if uids
                else (None if generation_changed else cursor.token if cursor else None)
            )
            return MailFetchBatch(
                messages=messages,
                next_cursor=MailCursor(
                    mailbox=self.config.mailbox,
                    token=next_token,
                    uid_validity=uid_validity or (cursor.uid_validity if cursor else None),
                ),
            )
        except TimeoutError as exc:
            raise MailConnectorError("imap_sync_timeout") from exc
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise MailConnectorError("imap_connection_failed") from exc
        finally:
            if client is not None:
                self._cleanup_client(client, deadline=deadline)

    @staticmethod
    def _start_uid(cursor: MailCursor | None, uid_validity: str | None = None) -> int:
        if cursor is None or cursor.token is None:
            return 1
        if (
            uid_validity is not None
            and cursor.uid_validity is not None
            and cursor.uid_validity != uid_validity
        ):
            return 1
        try:
            return int(cursor.token) + 1
        except ValueError as exc:
            raise ValueError("IMAP cursor token must be a numeric UID") from exc

    def _trusted_authserv_ids(self) -> set[str]:
        configured = {
            value.casefold().strip().rstrip(".")
            for value in self.config.trusted_authserv_ids
            if value.strip()
        }
        if configured:
            return configured
        host = self.config.host.casefold().rstrip(".")
        if host not in _NETEASE_PERSONAL_IMAP_HOSTS:
            return set()
        provider = host.removeprefix("imap.")
        return {provider, "coremail", "coremail.net"}

    def fetch_source_metadata(
        self,
        uid: str | int,
        *,
        expected_uid_validity: str | None = None,
    ) -> dict[str, Any]:
        """Read bounded headers for a known UID without fetching the message body."""

        uid_text = str(uid).strip()
        if not uid_text.isdigit() or int(uid_text) < 1:
            raise ValueError("IMAP UID must be a positive integer")
        client = None
        deadline = monotonic() + self.config.timeout_seconds
        try:
            context = ssl.create_default_context()
            client = self._open_client(
                context,
                timeout_seconds=max(0.001, deadline - monotonic()),
            )
            self._apply_deadline(client, deadline)
            client.login(self.config.username, self.config.password.get_secret_value())
            self._apply_deadline(client, deadline)
            _identify_netease_client(client, self.config.host)
            self._apply_deadline(client, deadline)
            status, select_data = client.select(self.config.mailbox, readonly=True)
            if status != "OK":
                raise MailConnectorError("imap_mailbox_unavailable")
            uid_validity = _uid_validity_from_response(client, select_data)
            if (
                expected_uid_validity is not None
                and uid_validity is not None
                and str(expected_uid_validity) != uid_validity
            ):
                raise MailConnectorError("imap_uidvalidity_changed")
            self._apply_deadline(client, deadline)
            status, data = client.uid(
                "fetch",
                uid_text.encode("ascii"),
                "(BODY.PEEK[HEADER])",
            )
            if status != "OK":
                raise MailConnectorError("imap_header_fetch_failed")
            raw = _raw_message(data)
            if raw is None:
                raise MailConnectorError("imap_header_fetch_empty")
            parsed = BytesParser(policy=policy.default).parsebytes(raw[: self.maximum_message_bytes])
            return _source_metadata(
                parsed,
                uid=uid_text,
                uid_validity=uid_validity,
                trusted_authserv_ids=self._trusted_authserv_ids(),
            )
        except TimeoutError as exc:
            raise MailConnectorError("imap_header_fetch_timeout") from exc
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise MailConnectorError("imap_connection_failed") from exc
        finally:
            if client is not None:
                self._cleanup_client(client, deadline=deadline)

    def find_exact_source_metadata(
        self,
        *,
        subject: str,
        received_at: datetime,
        body_text: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Find one legacy message by exact persisted content in a bounded date window."""

        if limit < 1 or limit > 100:
            raise ValueError("legacy mail search limit must be between 1 and 100")
        client = None
        deadline = monotonic() + self.config.timeout_seconds
        try:
            context = ssl.create_default_context()
            client = self._open_client(
                context,
                timeout_seconds=max(0.001, deadline - monotonic()),
            )
            self._apply_deadline(client, deadline)
            client.login(self.config.username, self.config.password.get_secret_value())
            self._apply_deadline(client, deadline)
            _identify_netease_client(client, self.config.host)
            self._apply_deadline(client, deadline)
            status, select_data = client.select(self.config.mailbox, readonly=True)
            if status != "OK":
                raise MailConnectorError("imap_mailbox_unavailable")
            uid_validity = _uid_validity_from_response(client, select_data)
            value = received_at if received_at.tzinfo is not None else received_at.replace(tzinfo=timezone.utc)
            start = (value - timedelta(days=1)).strftime("%d-%b-%Y")
            end = (value + timedelta(days=2)).strftime("%d-%b-%Y")
            self._apply_deadline(client, deadline)
            status, search_data = client.uid("search", None, f"SINCE {start} BEFORE {end}")
            if status != "OK":
                raise MailConnectorError("imap_search_failed")
            raw_uids = search_data[0].split() if search_data and search_data[0] else []
            if len(raw_uids) > limit:
                raise MailConnectorError("imap_legacy_candidate_limit_exceeded")
            from .preparation import prepare_mail_for_model

            matches: list[EmailMessage] = []
            for uid in raw_uids:
                message = self._fetch_message(
                    client,
                    uid,
                    uid_validity=uid_validity,
                    deadline=deadline,
                )
                if message is None or message.subject != subject:
                    continue
                parsed = prepare_mail_for_model(message)
                candidate_time = parsed.received_at
                if candidate_time is None:
                    continue
                if candidate_time.tzinfo is None:
                    candidate_time = candidate_time.replace(tzinfo=timezone.utc)
                if candidate_time == value and parsed.body_text == body_text:
                    matches.append(message)
            if len(matches) != 1:
                reason = "not_found" if not matches else "ambiguous"
                raise MailConnectorError(f"imap_legacy_exact_match_{reason}")
            return dict(matches[0].source_metadata)
        except TimeoutError as exc:
            raise MailConnectorError("imap_legacy_search_timeout") from exc
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise MailConnectorError("imap_connection_failed") from exc
        finally:
            if client is not None:
                self._cleanup_client(client, deadline=deadline)

    fetch_header_metadata = fetch_source_metadata

    def _fetch_message(
        self,
        client: imaplib.IMAP4_SSL,
        uid: bytes,
        *,
        uid_validity: str | None = None,
        deadline: float | None = None,
    ) -> EmailMessage | None:
        if deadline is not None:
            self._apply_deadline(client, deadline)
        status, data = client.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK":
            raise MailConnectorError("imap_fetch_failed")
        if deadline is not None:
            self._apply_deadline(client, deadline)
        raw = _raw_message(data)
        if raw is None:
            return None
        parsed = BytesParser(policy=policy.default).parsebytes(raw[: self.maximum_message_bytes])
        text_body, html_body = _body(parsed, maximum=self.maximum_message_bytes)
        received_at = None
        if parsed.get("Date"):
            try:
                received_at = parsedate_to_datetime(str(parsed["Date"]))
            except (TypeError, ValueError, OverflowError):
                received_at = None
        uid_text = uid.decode("ascii")
        message_id, _message_id_source = _header_message_id(parsed, uid_text)
        account_material = f"{self.config.host}\0{self.config.username}".encode("utf-8")
        account_ref = sha256(account_material).hexdigest()[:20]
        recipients = [
            str(parsed.get(name))
            for name in ("To", "Cc")
            if parsed.get(name)
        ]
        return EmailMessage(
            identity=MailIdentity(
                message_id=message_id,
                thread_id=str(parsed.get("References") or "").strip() or None,
                mailbox=self.config.mailbox,
                account_ref=account_ref,
            ),
            sender=str(parsed.get("From") or "").strip() or None,
            recipients=recipients,
            subject=str(parsed.get("Subject") or ""),
            body_text=text_body,
            html_body=html_body,
            received_at=received_at,
            source_metadata=_source_metadata(
                parsed,
                uid=uid_text,
                uid_validity=uid_validity,
                trusted_authserv_ids=self._trusted_authserv_ids(),
                mailbox_read=_mailbox_read_from_fetch(data),
            ),
            mailbox_read=_mailbox_read_from_fetch(data),
        )


__all__ = [
    "DEFAULT_IMAP_TIMEOUT_SECONDS",
    "ImapConnectionConfig",
    "ImapReadOnlyConnector",
    "MailConnector",
    "MailConnectorError",
    "MailFetchBatch",
]
