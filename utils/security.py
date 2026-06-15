"""
Passwordless request guards for ComfyUI-Deadline-Distributed.
"""
import hmac
import ipaddress
import secrets
from urllib.parse import urlparse

from aiohttp import web

from .config import load_config, save_config
from .logging import debug_log

JOB_TOKEN_HEADER = "X-Comfy-Distributed-Job-Token"
DEADLINE_TOKEN_HEADER = "X-Comfy-Deadline-Registration-Token"
JOB_TOKEN_FIELD = "job_token"
DEADLINE_TOKEN_FIELD = "registration_token"


def generate_token():
    return secrets.token_urlsafe(32)


def ensure_security_config():
    config = load_config()
    security = config.setdefault("security", {})
    changed = False

    if not security.get("instance_token"):
        security["instance_token"] = generate_token()
        changed = True

    defaults = {
        "require_private_network": True,
        "allow_missing_origin_from_private_network": True,
    }
    for key, value in defaults.items():
        if key not in security:
            security[key] = value
            changed = True

    if changed:
        save_config(config)
    return security


def get_request_token(request, field_name=JOB_TOKEN_FIELD, header_name=JOB_TOKEN_HEADER, data=None):
    token = request.headers.get(header_name)
    if token:
        return str(token)
    if data is not None:
        value = data.get(field_name)
        if value is not None:
            return str(value)
    return ""


def token_matches(provided, expected):
    if not provided or not expected:
        return False
    return hmac.compare_digest(str(provided), str(expected))


def make_job_headers(job_token):
    return {JOB_TOKEN_HEADER: str(job_token)} if job_token else {}


def make_deadline_headers(registration_token):
    return {DEADLINE_TOKEN_HEADER: str(registration_token)} if registration_token else {}


def is_private_or_loopback(host):
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return host in {"localhost", "127.0.0.1", "::1"}


def get_client_host(request):
    peername = request.transport.get_extra_info("peername") if request.transport else None
    if peername:
        return peername[0]
    return request.remote


def _host_without_port(value):
    parsed = urlparse(value if "://" in value else f"//{value}")
    return parsed.hostname, parsed.port


def is_same_origin_request(request):
    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")
    candidate = origin or referer
    if not candidate:
        fetch_site = request.headers.get("Sec-Fetch-Site", "").lower()
        if fetch_site in {"same-origin", "same-site", "none"}:
            return True
        if fetch_site == "cross-site":
            return False
        return None

    request_host, request_port = _host_without_port(request.host)
    origin_host, origin_port = _host_without_port(candidate)
    if not request_host or not origin_host:
        return False
    return request_host.lower() == origin_host.lower() and request_port == origin_port


def reject_unsafe_request(request, action="request"):
    security = ensure_security_config()
    same_origin = is_same_origin_request(request)
    client_host = get_client_host(request)
    private_client = is_private_or_loopback(client_host)

    if same_origin is False:
        debug_log(f"Rejected cross-origin {action} from {client_host}")
        return web.json_response({"status": "error", "message": "Cross-origin request rejected"}, status=403)

    if same_origin is None and security.get("require_private_network", True):
        if not (security.get("allow_missing_origin_from_private_network", True) and private_client):
            debug_log(f"Rejected {action} without origin from {client_host}")
            return web.json_response({"status": "error", "message": "Request origin rejected"}, status=403)

    return None


def require_job_token(request, expected_token, data=None):
    provided = get_request_token(request, data=data)
    if not token_matches(provided, expected_token):
        debug_log("Rejected distributed worker request with missing or invalid job token")
        return web.json_response({"status": "error", "message": "Invalid distributed job token"}, status=403)
    return None


def require_deadline_registration_token(request, expected_token, data=None):
    provided = get_request_token(
        request,
        field_name=DEADLINE_TOKEN_FIELD,
        header_name=DEADLINE_TOKEN_HEADER,
        data=data,
    )
    if not token_matches(provided, expected_token):
        debug_log("Rejected Deadline worker request with missing or invalid registration token")
        return web.json_response({"success": False, "error": "Invalid Deadline registration token"}, status=403)
    return None
