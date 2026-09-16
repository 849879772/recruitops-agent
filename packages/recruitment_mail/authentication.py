"""Evaluate authentication facts persisted by the trusted IMAP connector."""

AUTHENTICATION_VERSION = "recruitops.mail_auth.v3"


def has_aligned_authentication(metadata):
    if not isinstance(metadata, dict):
        return False
    for item in metadata.get("authentication_results", []):
        if not isinstance(item, dict) or not item.get("authserv_id"):
            continue
        if item.get("result") != "pass" or item.get("aligned") is not True:
            continue
        if item.get("method") == "dkim":
            return True
        if item.get("method") == "spf":
            domains = [metadata.get("sender_domain"), metadata.get("return_path_domain"), item.get("identity_domain")]
            if all(isinstance(d, str) and "." in d for d in domains):
                if len({d.lower().rstrip(".") for d in domains}) == 1:
                    return True
    return False
