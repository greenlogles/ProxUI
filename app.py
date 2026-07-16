import base64
import json
import math
import os
import pprint
import re
import secrets
import shlex
import socket
import ssl
import threading
import time
import traceback
import uuid
from collections import defaultdict
from datetime import datetime
from functools import wraps
from urllib.parse import quote, unquote, urlparse

import requests
import toml
import urllib3
import websocket
import yaml
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sock import Sock
from proxmoxer import ProxmoxAPI

# Disable SSL warnings if needed
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Default timeout for Proxmox API operations (in seconds)
PROXMOX_TIMEOUT = 30

# Retry configuration for slow operations
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY = 2  # seconds
RETRY_MAX_DELAY = 30  # seconds


def retry_on_timeout(
    func,
    *args,
    max_attempts=RETRY_MAX_ATTEMPTS,
    base_delay=RETRY_BASE_DELAY,
    job_id=None,
    job_queue_ref=None,
    operation_name="operation",
    **kwargs,
):
    """
    Retry a function on timeout errors with exponential backoff.

    Args:
        func: The function to call
        *args: Positional arguments for the function
        max_attempts: Maximum number of retry attempts
        base_delay: Initial delay between retries (doubles each attempt)
        job_id: Optional job ID for logging to job queue
        job_queue_ref: Optional reference to job queue for logging
        operation_name: Name of the operation for logging
        **kwargs: Keyword arguments for the function

    Returns:
        The result of the function call

    Raises:
        The last exception if all retries fail
    """
    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_str = str(e).lower()
            # Check if it's a timeout-related error
            is_timeout = any(
                x in error_str
                for x in [
                    "timed out",
                    "timeout",
                    "read timeout",
                    "connection timeout",
                    "connecttimeout",
                ]
            )

            if not is_timeout:
                # Not a timeout error, re-raise immediately
                raise

            last_exception = e

            if attempt < max_attempts:
                # Calculate delay with exponential backoff
                delay = min(base_delay * (2 ** (attempt - 1)), RETRY_MAX_DELAY)

                log_msg = f"Timeout on {operation_name} (attempt {attempt}/{max_attempts}). Retrying in {delay}s..."
                print(log_msg)

                if job_id and job_queue_ref:
                    job_queue_ref.add_step(job_id, log_msg)

                time.sleep(delay)
            else:
                log_msg = f"Timeout on {operation_name} after {max_attempts} attempts. Giving up."
                print(log_msg)
                if job_id and job_queue_ref:
                    job_queue_ref.add_step(job_id, log_msg)

    # All retries exhausted
    raise last_exception


app = Flask(__name__)


def _get_secret_key():
    secret_key = os.environ.get("PROXUI_SECRET_KEY") or os.environ.get("SECRET_KEY")
    if secret_key:
        return secret_key

    print(
        "WARNING: PROXUI_SECRET_KEY is not set; using a temporary generated "
        "secret key. Set PROXUI_SECRET_KEY for stable, secure sessions."
    )
    return secrets.token_hex(32)


app.secret_key = _get_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=os.environ.get("PROXUI_SESSION_COOKIE_SAMESITE", "Lax"),
    SESSION_COOKIE_SECURE=os.environ.get("PROXUI_SESSION_COOKIE_SECURE", "").lower()
    in ("1", "true", "yes"),
)

CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ALLOWED_DOWNLOAD_SCHEMES = {"http", "https"}


def app_auth_credentials():
    username = os.environ.get("PROXUI_AUTH_USERNAME", "").strip()
    password = os.environ.get("PROXUI_AUTH_PASSWORD", "")
    if username and password:
        return username, password
    return None, None


def app_auth_challenge():
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="ProxUI"'},
    )


@app.before_request
def require_app_auth():
    username, password = app_auth_credentials()
    if not username or not password or request.endpoint == "static":
        return

    auth = request.authorization
    if not auth:
        return app_auth_challenge()

    username_ok = secrets.compare_digest(auth.username or "", username)
    password_ok = secrets.compare_digest(auth.password or "", password)
    if not username_ok or not password_ok:
        return app_auth_challenge()


def csrf_protection_enabled():
    if app.config.get("TESTING") and not app.config.get("ENABLE_CSRF_IN_TESTS"):
        return False
    return app.config.get("WTF_CSRF_ENABLED", True)


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def validate_csrf_token():
    if (
        not csrf_protection_enabled()
        or request.method in CSRF_SAFE_METHODS
        or request.endpoint == "static"
    ):
        return

    sent_token = (
        request.form.get("_csrf_token")
        or request.headers.get("X-CSRF-Token")
        or request.headers.get("X-CSRFToken")
    )
    expected_token = session.get("_csrf_token")

    if not sent_token or not expected_token:
        if request.path.startswith("/api/"):
            return jsonify({"error": "Missing CSRF token"}), 400
        abort(400)

    if not secrets.compare_digest(sent_token, expected_token):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Invalid CSRF token"}), 400
        abort(400)


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


def is_allowed_download_url(url):
    if not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False

    if parsed.scheme.lower() not in ALLOWED_DOWNLOAD_SCHEMES:
        return False
    if not parsed.netloc:
        return False
    if parsed.username or parsed.password:
        return False
    return True


def filename_from_url(url):
    try:
        path = urlparse(url.strip()).path
    except ValueError:
        return ""
    return path.rsplit("/", 1)[-1]


def is_safe_download_filename(filename, allowed_suffixes):
    if not isinstance(filename, str):
        return False

    filename = filename.strip()
    if not filename or filename in {".", ".."}:
        return False
    if "/" in filename or "\\" in filename or "\x00" in filename:
        return False
    if ".." in filename:
        return False
    if any(ord(char) < 32 for char in filename):
        return False
    if not filename.endswith(allowed_suffixes):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._ -]+", filename))


# Initialize Flask-Sock for WebSocket support
sock = Sock(app)

# =============================================================================
# VNC Proxy Session Store
# =============================================================================

vnc_sessions = {}  # session_id -> {ticket, port, node, vmid, created_at, proxmox_host}
vnc_sessions_lock = threading.Lock()
VNC_SESSION_TIMEOUT = 300  # 5 minutes

lxc_config_backups = (
    {}
)  # "node:vmid" -> {"config": {...}, "timestamp": "...", "features": "..."}


def cleanup_expired_vnc_sessions():
    """Remove expired VNC sessions"""
    now = time.time()
    with vnc_sessions_lock:
        expired = [
            sid
            for sid, data in vnc_sessions.items()
            if now - data["created_at"] > VNC_SESSION_TIMEOUT
        ]
        for sid in expired:
            del vnc_sessions[sid]


# =============================================================================
# Background Job Queue System
# =============================================================================


class JobQueue:
    """Simple in-memory job queue for background tasks"""

    def __init__(self):
        self.jobs = {}  # job_id -> job_info
        self.lock = threading.Lock()

    def create_job(self, job_type, description, params):
        """Create a new job and return its ID"""
        job_id = str(uuid.uuid4())[:8]
        job = {
            "id": job_id,
            "type": job_type,
            "description": description,
            "params": params,
            "status": "queued",  # queued, running, completed, failed
            "progress": 0,
            "current_step": "",
            "steps": [],
            "result": None,
            "error": None,
            "created_at": datetime.now().isoformat(),
            "started_at": None,
            "completed_at": None,
        }
        with self.lock:
            self.jobs[job_id] = job
        return job_id

    def get_job(self, job_id):
        """Get job info by ID"""
        with self.lock:
            return self.jobs.get(job_id)

    def get_all_jobs(self, limit=50):
        """Get all jobs, most recent first"""
        with self.lock:
            jobs = list(self.jobs.values())
        jobs.sort(key=lambda x: x["created_at"], reverse=True)
        return jobs[:limit]

    def update_job(self, job_id, **kwargs):
        """Update job fields"""
        with self.lock:
            if job_id in self.jobs:
                self.jobs[job_id].update(kwargs)

    def add_step(self, job_id, step_msg):
        """Add a step message to the job log"""
        with self.lock:
            if job_id in self.jobs:
                timestamp = datetime.now().strftime("%H:%M:%S")
                self.jobs[job_id]["steps"].append(f"[{timestamp}] {step_msg}")
                self.jobs[job_id]["current_step"] = step_msg

    def set_running(self, job_id):
        """Mark job as running"""
        self.update_job(job_id, status="running", started_at=datetime.now().isoformat())

    def set_completed(self, job_id, result=None):
        """Mark job as completed"""
        self.update_job(
            job_id,
            status="completed",
            progress=100,
            result=result,
            completed_at=datetime.now().isoformat(),
            current_step="Completed",
        )

    def set_failed(self, job_id, error):
        """Mark job as failed"""
        self.update_job(
            job_id,
            status="failed",
            error=str(error),
            completed_at=datetime.now().isoformat(),
            current_step=f"Failed: {error}",
        )

    def delete_job(self, job_id):
        """Delete a job"""
        with self.lock:
            if job_id in self.jobs:
                del self.jobs[job_id]
                return True
        return False

    def cleanup_old_jobs(self, max_age_hours=24):
        """Remove jobs older than max_age_hours"""
        cutoff = datetime.now().timestamp() - (max_age_hours * 3600)
        with self.lock:
            to_delete = []
            for job_id, job in self.jobs.items():
                created = datetime.fromisoformat(job["created_at"]).timestamp()
                if created < cutoff and job["status"] in ["completed", "failed"]:
                    to_delete.append(job_id)
            for job_id in to_delete:
                del self.jobs[job_id]


# Global job queue instance
job_queue = JobQueue()


# Custom Jinja filters
def humanize_uptime(seconds):
    """Convert uptime seconds to human readable format"""
    if not seconds or seconds == 0:
        return "N/A"

    try:
        seconds = int(float(seconds))
    except (ValueError, TypeError):
        return "N/A"

    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m {secs}s"
    elif seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = ((seconds % 86400) % 3600) // 60
        return f"{days}d {hours}h {minutes}m"


app.jinja_env.filters["humanize_uptime"] = humanize_uptime


def humanize_bytes(num_bytes, decimals=1):
    """Convert a byte count to an adaptive-unit string (B/KB/MB/GB/TB/PB),
    keeping the numeric value between roughly 1.0 and 999.9."""
    try:
        num_bytes = float(num_bytes)
    except (ValueError, TypeError):
        return "N/A"
    if num_bytes <= 0:
        return "0 B"

    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    k = 1024
    unit_index = int(math.floor(math.log(num_bytes, k)))
    unit_index = max(0, min(unit_index, len(units) - 1))
    value = num_bytes / (k**unit_index)

    if unit_index == 0:
        return f"{int(value)} B"
    return f"{value:.{decimals}f} {units[unit_index]}"


app.jinja_env.filters["humanize_bytes"] = humanize_bytes

# Configuration file path for Docker volume
import os

# Try Docker path first, fallback to local data directory
try:
    CONFIG_FILE_PATH = os.environ.get("CONFIG_FILE_PATH", "/app/data/config.toml")
    os.makedirs(os.path.dirname(CONFIG_FILE_PATH), exist_ok=True)
except PermissionError:
    # Fallback to local data directory for development
    CONFIG_FILE_PATH = "./data/config.toml"
    os.makedirs(os.path.dirname(CONFIG_FILE_PATH), exist_ok=True)

# Load configuration
config_file_exists = True
try:
    with open(CONFIG_FILE_PATH, "r") as f:
        config = toml.load(f)
        if not config.get("clusters"):
            config_file_exists = False
            config = {"clusters": []}
except Exception as e:
    print(f"Error loading {CONFIG_FILE_PATH}: {e}")
    config_file_exists = False
    config = {"clusters": []}


def save_config_file(config_data):
    with open(CONFIG_FILE_PATH, "w") as f:
        toml.dump(config_data, f)
    try:
        os.chmod(CONFIG_FILE_PATH, 0o600)
    except OSError:
        pass


# App version — set via PROXUI_VERSION env var (Docker build arg),
# falls back to short git commit hash for local development
def _get_app_version():
    version = os.environ.get("PROXUI_VERSION", "").strip()
    if version:
        return version
    try:
        import subprocess

        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except Exception:
        return "dev"


APP_VERSION = _get_app_version()

# Demo mode configuration
# Can be enabled via environment variable or config file
DEMO_MODE = os.environ.get("PROXUI_DEMO_MODE", "").lower() in ("1", "true", "yes")
if not DEMO_MODE:
    DEMO_MODE = config.get("demo_mode", False)

DEMO_MESSAGE = os.environ.get(
    "PROXUI_DEMO_MESSAGE",
    config.get(
        "demo_message",
        "This is a demo instance with read-only access. You can explore all features, but changes will not be saved due to permission restrictions.",
    ),
)

if DEMO_MODE:
    print(f"Demo mode enabled: {DEMO_MESSAGE}")

# Cloud image definitions for VM templates
# Load cloud images from external JSON file
# Can be overridden via CLOUD_IMAGES_PATH environment variable
CLOUD_IMAGES_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cloud_images.json"
)
CLOUD_IMAGES_FILE = os.environ.get("CLOUD_IMAGES_PATH", CLOUD_IMAGES_DEFAULT_PATH)

# Control behavior when cloud image already exists on storage
# REUSE (default): Skip download if image exists, use existing file
# OVERWRITE: Delete existing file and download fresh copy
CLOUD_IMAGE_CACHE_MODE = os.environ.get("CLOUD_IMAGE_CACHE", "REUSE").upper()


def load_cloud_images():
    """Load cloud images configuration from JSON file

    The file path can be overridden by setting the CLOUD_IMAGES_PATH environment variable.
    This is useful for container deployments where a custom images list can be mounted.
    """
    try:
        with open(CLOUD_IMAGES_FILE, "r") as f:
            images = json.load(f)
            print(f"Loaded {len(images)} cloud images from {CLOUD_IMAGES_FILE}")
            return images
    except FileNotFoundError:
        print(f"Warning: Cloud images file not found: {CLOUD_IMAGES_FILE}")
        return {}
    except json.JSONDecodeError as e:
        print(f"Warning: Invalid JSON in cloud images file: {e}")
        return {}


CLOUD_IMAGES = load_cloud_images()

LXC_PROFILES_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "lxc_profiles.json"
)
LXC_PROFILES_FILE = os.environ.get("LXC_PROFILES_PATH", LXC_PROFILES_DEFAULT_PATH)


def load_lxc_profiles():
    """Load LXC profile bundles from JSON file (path overridable via LXC_PROFILES_PATH)."""
    try:
        with open(LXC_PROFILES_FILE, "r") as f:
            profiles = json.load(f)
            print(f"Loaded {len(profiles)} LXC profiles from {LXC_PROFILES_FILE}")
            return profiles
    except FileNotFoundError:
        print(f"Warning: LXC profiles file not found: {LXC_PROFILES_FILE}")
        return {}
    except json.JSONDecodeError as e:
        print(f"Warning: Invalid JSON in LXC profiles file: {e}")
        return {}


LXC_PROFILES = load_lxc_profiles()

# Store Proxmox connections for multiple clusters
all_clusters = {}  # cluster_id -> cluster config
current_cluster_id = None
proxmox_nodes = {}  # node_name -> proxmox connection for current cluster
cluster_nodes = []  # Store all nodes from current cluster
connection_metadata = {}  # Store connection metadata for renewal


def create_proxmox_connection(node_config, timeout=None):
    """Create a ProxmoxAPI connection from node config, supporting both password and API token auth

    Args:
        node_config: Dictionary with connection settings (host, user, password/token, etc.)
        timeout: Request timeout in seconds (default: PROXMOX_TIMEOUT)

    Returns:
        ProxmoxAPI instance
    """
    host = node_config["host"]
    user = node_config["user"]
    verify_ssl = node_config.get("verify_ssl", True)
    req_timeout = timeout if timeout is not None else PROXMOX_TIMEOUT

    # Check if using API token authentication
    if node_config.get("token_name") and node_config.get("token_value"):
        return ProxmoxAPI(
            host,
            user=user,
            token_name=node_config["token_name"],
            token_value=node_config["token_value"],
            verify_ssl=verify_ssl,
            timeout=req_timeout,
        )
    else:
        # Password-based authentication
        return ProxmoxAPI(
            host,
            user=user,
            password=node_config["password"],
            verify_ssl=verify_ssl,
            timeout=req_timeout,
        )


def init_all_clusters():
    """Initialize all cluster configurations"""
    global all_clusters, current_cluster_id

    all_clusters = {}

    # Load all clusters from config
    if "clusters" in config:
        for cluster_config in config["clusters"]:
            cluster_id = cluster_config.get("id", "default")
            all_clusters[cluster_id] = cluster_config

    # Set default cluster if none specified
    if all_clusters and not current_cluster_id:
        current_cluster_id = list(all_clusters.keys())[0]

    print(f"Loaded {len(all_clusters)} cluster(s): {list(all_clusters.keys())}")


def init_proxmox_connections(cluster_id=None):
    """Initialize connections to Proxmox nodes for specified cluster"""
    global cluster_nodes, proxmox_nodes, current_cluster_id, connection_metadata

    if cluster_id:
        current_cluster_id = cluster_id

    if not current_cluster_id or current_cluster_id not in all_clusters:
        print("No valid cluster selected")
        return False

    # Clear existing connections
    proxmox_nodes = {}
    cluster_nodes = []
    connection_metadata = {}

    cluster_config = all_clusters[current_cluster_id]
    discovered_nodes = set()

    print(
        f"Initializing connections for cluster: {cluster_config.get('name', current_cluster_id)}"
    )

    # Connect to configured nodes for this cluster
    for node_config in cluster_config.get("nodes", []):
        try:
            proxmox = create_proxmox_connection(node_config)

            # Test connection
            version = proxmox.version.get()
            print(f"Connected to {node_config['host']} - PVE {version['version']}")

            # Store connection metadata for renewal
            metadata = {
                "host": node_config["host"],
                "user": node_config["user"],
                "verify_ssl": node_config.get("verify_ssl", True),
                "last_authenticated": datetime.now(),
                "pve_version": version.get("version", ""),
            }
            # Store auth credentials (token or password)
            if node_config.get("token_name") and node_config.get("token_value"):
                metadata["token_name"] = node_config["token_name"]
                metadata["token_value"] = node_config["token_value"]
            else:
                metadata["password"] = node_config["password"]
            connection_metadata[node_config["host"]] = metadata

            # Get all nodes in the cluster through this connection
            try:
                nodes = proxmox.nodes.get()
                for node in nodes:
                    node_name = node["node"]
                    if node_name not in discovered_nodes:
                        discovered_nodes.add(node_name)
                        # Store the connection that can access this node
                        if node_name not in proxmox_nodes:
                            proxmox_nodes[node_name] = proxmox
                            # Store metadata for each node
                            if node_name not in connection_metadata:
                                connection_metadata[node_name] = connection_metadata[
                                    node_config["host"]
                                ]
                        cluster_nodes.append(
                            {
                                "name": node_name,
                                "status": node["status"],
                                "connection": proxmox,
                            }
                        )
            except Exception as e:
                print(f"Error getting cluster nodes from {node_config['host']}: {e}")
                # If cluster endpoint fails, at least add this node
                proxmox_nodes[node_config["host"]] = proxmox

        except Exception as e:
            print(f"Failed to connect to {node_config['host']}: {e}")

    print(
        f"Discovered {len(cluster_nodes)} cluster nodes: {[n['name'] for n in cluster_nodes]}"
    )
    print(f"Active connections to {len(proxmox_nodes)} nodes")
    return len(proxmox_nodes) > 0


def is_authentication_error(error):
    """Check if an error is related to authentication/authorization"""
    error_str = str(error).lower()
    auth_indicators = [
        "couldn't authenticate",
        "authentication failed",
        "401",
        "unauthorized",
        "permission denied",
        "ticket",
        "authentication failure",
        "invalid ticket",
        "authentication required",
    ]
    return any(indicator in error_str for indicator in auth_indicators)


def renew_proxmox_connection(node_name):
    """Renew a Proxmox connection for a specific node"""
    global proxmox_nodes, cluster_nodes, connection_metadata

    if node_name not in connection_metadata:
        print(f"No connection metadata found for node {node_name}")
        return None

    metadata = connection_metadata[node_name]

    try:
        print(f"Renewing connection for node {node_name}...")

        # Create new connection using stored metadata
        new_proxmox = create_proxmox_connection(metadata)

        # Test the new connection
        version = new_proxmox.version.get()
        print(
            f"Successfully renewed connection to {node_name} - PVE {version['version']}"
        )

        # Update the stored connection
        proxmox_nodes[node_name] = new_proxmox

        # Update cluster_nodes list
        for node_info in cluster_nodes:
            if node_info["name"] == node_name:
                node_info["connection"] = new_proxmox
                break

        # Update metadata timestamp
        metadata["last_authenticated"] = datetime.now()

        return new_proxmox

    except Exception as e:
        print(f"Failed to renew connection for {node_name}: {e}")
        return None


def get_proxmox_connection(node_name, auto_renew=True):
    """Get a Proxmox connection with automatic renewal on auth errors"""
    connection = get_proxmox_for_node(node_name)

    if not connection:
        return None

    # If auto_renew is disabled, return the connection as-is
    if not auto_renew:
        return connection

    # Test the connection with a simple API call
    try:
        connection.version.get()
        return connection
    except Exception as e:
        if is_authentication_error(e):
            print(
                f"Authentication error detected for {node_name}, attempting renewal..."
            )
            renewed_connection = renew_proxmox_connection(node_name)
            if renewed_connection:
                return renewed_connection
            else:
                print(f"Failed to renew connection for {node_name}")
                return None
        else:
            # Non-authentication error, return original connection
            return connection


def proxmox_api_call(connection, api_func, *args, **kwargs):
    """Execute a Proxmox API call with automatic retry on authentication errors"""
    max_retries = 2

    for attempt in range(max_retries):
        try:
            return api_func(*args, **kwargs)
        except Exception as e:
            if is_authentication_error(e) and attempt < max_retries - 1:
                print(f"Authentication error on attempt {attempt + 1}, retrying...")
                # Find the node name for this connection
                node_name = None
                for name, conn in proxmox_nodes.items():
                    if conn == connection:
                        node_name = name
                        break

                if node_name:
                    renewed_connection = renew_proxmox_connection(node_name)
                    if renewed_connection:
                        connection = renewed_connection
                        # Update the api_func if it's bound to the old connection
                        continue

            # If we get here, either it's not an auth error or renewal failed
            raise e

    return None


def update_cluster_next_id(proxmox, vmid):
    """Update the cluster's next-id lower bound after VM/container creation.

    This prevents VMID reuse when incremental_vmid is enabled for the cluster.
    It sets the next-id lower bound to vmid + 1, but only if this would
    increase the lower bound (not decrease it).

    Args:
        proxmox: Proxmox API connection
        vmid: The VMID that was just created
    """
    try:
        # Check if incremental_vmid is enabled for current cluster
        if current_cluster_id and current_cluster_id in all_clusters:
            cluster_config = all_clusters[current_cluster_id]
            if not cluster_config.get("incremental_vmid", False):
                return  # Feature not enabled

            # Get current cluster options to preserve existing bounds
            try:
                options = proxmox.cluster.options.get()
                current_next_id = options.get("next-id", "")
            except Exception:
                current_next_id = ""

            # Parse existing lower and upper bounds
            current_lower = None
            upper_bound = None
            if current_next_id:
                # Format: "lower=X,upper=Y" or just values
                for part in current_next_id.split(","):
                    part = part.strip()
                    if part.startswith("lower="):
                        try:
                            current_lower = int(part.split("=")[1])
                        except (ValueError, IndexError):
                            pass
                    elif part.startswith("upper="):
                        try:
                            upper_bound = int(part.split("=")[1])
                        except (ValueError, IndexError):
                            pass

            # Calculate new lower bound
            new_lower = int(vmid) + 1

            # Only update if new lower bound is greater than current
            if current_lower is not None and new_lower <= current_lower:
                print(
                    f"Skipping next-id update: new lower {new_lower} <= "
                    f"current lower {current_lower}"
                )
                return

            # Build new next-id value
            if upper_bound:
                next_id_value = f"lower={new_lower},upper={upper_bound}"
            else:
                next_id_value = f"lower={new_lower}"

            # Update cluster options
            proxmox.cluster.options.put(**{"next-id": next_id_value})
            print(f"Updated cluster next-id lower bound to {new_lower}")

    except Exception as e:
        # Don't fail VM creation if next-id update fails
        print(f"Warning: Failed to update cluster next-id: {e}")


def get_proxmox_for_node(node_name):
    """Get the appropriate Proxmox connection for a specific node"""
    # Self-heal: if we have a configured cluster but no live connections
    # (e.g. after a process restart / reloader cycle), re-establish them
    # lazily so requests don't fail with a storm of "Node not found" 404s.
    if not proxmox_nodes and current_cluster_id and current_cluster_id in all_clusters:
        print("No active Proxmox connections; re-initializing for current cluster...")
        init_proxmox_connections()

    # First check if we have a direct connection to this node
    if node_name in proxmox_nodes:
        return proxmox_nodes[node_name]

    # Otherwise, find a connection that can access this node
    for node_info in cluster_nodes:
        if node_info["name"] == node_name:
            return node_info["connection"]

    # Fallback to any available connection (they should all work in a cluster)
    if proxmox_nodes:
        return next(iter(proxmox_nodes.values()))

    return None


def get_all_vms_and_containers():
    """Get all VMs and containers from all nodes using cluster resources endpoint"""
    all_resources = []
    processed_vmids = set()  # Track processed VMs to avoid duplicates

    # Use the first available connection to get cluster-wide resources
    if proxmox_nodes:
        try:
            # Get any working connection with auto-renewal
            node_name = next(iter(proxmox_nodes.keys()))
            proxmox = get_proxmox_connection(node_name, auto_renew=True)

            if not proxmox:
                print("No valid Proxmox connection available")
                return []

            # Get all VMs and containers from cluster endpoint
            resources = proxmox.cluster.resources.get(type="vm")

            for resource in resources:
                # Skip if we've already processed this VM
                vm_key = f"{resource['node']}-{resource['vmid']}"
                if vm_key in processed_vmids:
                    continue
                processed_vmids.add(vm_key)

                # Calculate resource usage percentages
                if resource.get("maxcpu"):
                    resource["cpu_percent"] = resource.get("cpu", 0) * 100
                else:
                    resource["cpu_percent"] = 0

                if resource.get("maxmem") and resource.get("maxmem") > 0:
                    resource["mem_percent"] = (
                        resource.get("mem", 0) / resource.get("maxmem", 1)
                    ) * 100
                else:
                    resource["mem_percent"] = 0

                if resource.get("maxdisk") and resource.get("maxdisk") > 0:
                    resource["disk_percent"] = (
                        resource.get("disk", 0) / resource.get("maxdisk", 1)
                    ) * 100
                else:
                    resource["disk_percent"] = 0

                all_resources.append(resource)

        except Exception as e:
            print(f"Error getting cluster resources: {e}")
            # Fallback to node-by-node retrieval
            return get_all_vms_and_containers_fallback()

    return all_resources


def get_all_vms_and_containers_fallback():
    """Fallback method to get VMs/containers node by node"""
    all_resources = []
    processed_vmids = set()

    for node_info in cluster_nodes:
        try:
            node_name = node_info["name"]
            proxmox = get_proxmox_connection(node_name, auto_renew=True)

            if not proxmox:
                print(f"No valid connection for node {node_name}")
                continue

            # Get VMs
            vms = proxmox.nodes(node_name).qemu.get()
            for vm in vms:
                vm_key = f"{node_name}-{vm['vmid']}"
                if vm_key not in processed_vmids:
                    processed_vmids.add(vm_key)
                    vm["node"] = node_name
                    vm["type"] = "qemu"
                    # Calculate percentages
                    vm["cpu_percent"] = vm.get("cpu", 0) * 100 if vm.get("cpu") else 0
                    vm["mem_percent"] = (
                        (vm.get("mem", 0) / vm.get("maxmem", 1)) * 100
                        if vm.get("maxmem")
                        else 0
                    )
                    vm["disk_percent"] = (
                        (vm.get("disk", 0) / vm.get("maxdisk", 1)) * 100
                        if vm.get("maxdisk")
                        else 0
                    )
                    all_resources.append(vm)

            # Get containers
            containers = proxmox.nodes(node_name).lxc.get()
            for container in containers:
                ct_key = f"{node_name}-{container['vmid']}"
                if ct_key not in processed_vmids:
                    processed_vmids.add(ct_key)
                    container["node"] = node_name
                    container["type"] = "lxc"
                    # Calculate percentages
                    container["cpu_percent"] = (
                        container.get("cpu", 0) * 100 if container.get("cpu") else 0
                    )
                    container["mem_percent"] = (
                        (container.get("mem", 0) / container.get("maxmem", 1)) * 100
                        if container.get("maxmem")
                        else 0
                    )
                    container["disk_percent"] = (
                        (container.get("disk", 0) / container.get("maxdisk", 1)) * 100
                        if container.get("maxdisk")
                        else 0
                    )
                    all_resources.append(container)

        except Exception as e:
            print(f"Error getting resources from node {node_info['name']}: {e}")

    return all_resources


def get_qemu_guest_disk_info(proxmox, node, vmid):
    """Get disk usage information from QEMU guest agent"""
    try:
        # Check if guest agent is available by trying to get filesystem info
        fsinfo = proxmox.nodes(node).qemu(vmid).agent.get("get-fsinfo")
        disk_info = []
        # Filter some filesystems
        ignore_fs_type = ["squashfs"]
        if isinstance(fsinfo.get("result"), list):
            for fs in fsinfo.get("result"):
                # Extract relevant filesystem information
                if not fs.get("type") in ignore_fs_type:
                    disk_info.append(
                        {
                            "name": fs.get("name", "Unknown"),
                            "mountpoint": fs.get("mountpoint", "/"),
                            "type": fs.get("type", "unknown"),
                            "used_bytes": fs.get("used-bytes", 0),
                            "total_bytes": fs.get("total-bytes", 0),
                            "disk_name": (
                                fs.get("disk", [{}])[0].get("serial", "Unknown")
                                if fs.get("disk")
                                else "Unknown"
                            ),
                        }
                    )

        # Calculate usage percentages
        for disk in disk_info:
            if disk["total_bytes"] > 0:
                disk["used_percent"] = (disk["used_bytes"] / disk["total_bytes"]) * 100
                disk["used_gb"] = disk["used_bytes"] / (1024**3)
                disk["total_gb"] = disk["total_bytes"] / (1024**3)
                disk["free_gb"] = (disk["total_bytes"] - disk["used_bytes"]) / (1024**3)
            else:
                disk["used_percent"] = 0
                disk["used_gb"] = 0
                disk["total_gb"] = 0
                disk["free_gb"] = 0

        return disk_info

    except Exception as e:
        print(f"Error getting guest agent disk info: {e}")
        return None


def get_lxc_disk_info(status):
    """Build rootfs disk-usage info for an LXC container from status/current.

    Containers have no guest agent, but the host reports the container's rootfs
    usage directly — the same value 'pct df' shows for the rootfs mountpoint.
    Only the rootfs is exposed via the API; per-mountpoint usage (mp0, mp1, …)
    that 'pct df' also prints has no REST equivalent.

    Returns a one-element list shaped like get_qemu_guest_disk_info so the same
    template UI can render it, or None when usage is unavailable.
    """
    total_bytes = status.get("maxdisk") or 0
    used_bytes = status.get("disk") or 0
    if total_bytes <= 0:
        return None

    return [
        {
            "name": "rootfs",
            "mountpoint": "/",
            "type": "rootfs",
            "used_bytes": used_bytes,
            "total_bytes": total_bytes,
            "disk_name": "rootfs",
            "used_percent": (used_bytes / total_bytes) * 100,
            "used_gb": used_bytes / (1024**3),
            "total_gb": total_bytes / (1024**3),
            "free_gb": (total_bytes - used_bytes) / (1024**3),
        }
    ]


def is_agent_enabled(config):
    """True if a VM config's 'agent' value has the guest agent enabled.

    Handles the '1', '0', '1,fstrim_cloned_disks=1' and 'enabled=1,...' forms.
    """
    first = str(config.get("agent", "0")).split(",")[0].strip()
    if first.startswith("enabled="):
        first = first.split("=", 1)[1].strip()
    return first == "1"


def apply_agent_after_clone(source_node, target_node, target_vmid, task_upid, enabled):
    """Best-effort background apply of the guest-agent flag to a fresh clone.

    A clone inherits the source's 'agent' setting, and the target VM stays locked
    until the clone task finishes — so wait for the task, then set the flag. The
    create/clone default is disabled so the detail page never blocks waiting on a
    guest agent that isn't installed yet.
    """
    try:
        proxmox = get_proxmox_connection(source_node, auto_renew=True)
        if not proxmox:
            return
        waited = 0
        while waited < 1800:
            try:
                st = proxmox.nodes(source_node).tasks(task_upid).status.get()
                if st.get("status") == "stopped":
                    break
            except Exception:
                pass
            time.sleep(3)
            waited += 3

        conn = get_proxmox_connection(target_node, auto_renew=True) or proxmox
        # Preserve any extra agent options already present on the clone.
        try:
            cfg = conn.nodes(target_node).qemu(target_vmid).config.get()
            extra = [p for p in str(cfg.get("agent", "")).split(",")[1:] if p.strip()]
        except Exception:
            extra = []
        flag = "1" if enabled else "0"
        agent_val = ",".join([flag] + extra) if extra else flag
        conn.nodes(target_node).qemu(target_vmid).config.put(agent=agent_val)
    except Exception as e:
        print(f"Post-clone agent config failed for {target_vmid}: {e}")


def spawn_agent_after_clone(source_node, target_node, target_vmid, task_upid, enabled):
    """Run apply_agent_after_clone in a daemon thread (non-blocking)."""
    threading.Thread(
        target=apply_agent_after_clone,
        args=(source_node, target_node, target_vmid, task_upid, enabled),
        daemon=True,
    ).start()


def parse_vm_configuration(config, vm_type="qemu"):
    """Parse VM/LXC configuration into structured groups"""
    parsed_config = {
        "cpu": {},
        "memory": {},
        "network": [],
        "storage": [],
        "disks": [],
        "cdrom": [],
        "cloudinit_drive": None,
        "devices": [],
        "cloud_init": {},
        "general": {},
        "other": {},
    }

    for key, value in config.items():
        # CPU Configuration
        if key in ["cores", "sockets", "vcpus", "cpu", "cpulimit", "cpuunits"]:
            parsed_config["cpu"][key] = value

        # Memory Configuration
        elif key in ["memory", "balloon", "shares"]:
            parsed_config["memory"][key] = value

        # Network Configuration
        elif key.startswith("net"):
            net_info = {"interface": key, "config": value}
            if "=" in str(value):
                # Parse network config like "virtio,bridge=vmbr0,firewall=1"
                parts = str(value).split(",")
                if "=" in parts[0]:
                    net_info["model"] = parts[0].split("=")[0]
                    net_info["mac"] = parts[0].split("=")[1]
                else:
                    net_info["model"] = parts[0] if parts else "unknown"
                    net_info["mac"] = "unknown"
                for part in parts[1:]:
                    if "=" in part:
                        k, v = part.split("=", 1)
                        net_info[k] = v
            parsed_config["network"].append(net_info)

        # Storage Configuration
        elif key.startswith(("scsi", "ide", "sata", "virtio", "rootfs", "mp")):
            storage_info = {"device": key, "config": value}
            value_str = str(value)
            if "=" in value_str or ":" in value_str:
                # Parse storage config like "local-lvm:vm-100-disk-0,size=32G"
                storage_info["details"] = value_str

            # Categorize storage by type
            if "cloudinit" in value_str.lower():
                # Cloud-init drive
                parsed_config["cloudinit_drive"] = storage_info
            elif ".iso" in value_str.lower() or "media=cdrom" in value_str.lower():
                # CD/DVD/ISO image
                # Extract ISO filename if present
                if ".iso" in value_str.lower():
                    parts = value_str.split("/")
                    for part in parts:
                        if ".iso" in part.lower():
                            iso_name = part.split(",")[0]
                            storage_info["iso_name"] = iso_name
                            break
                parsed_config["cdrom"].append(storage_info)
            else:
                # Regular disk
                # Extract size if present
                if "size=" in value_str:
                    for part in value_str.split(","):
                        if part.startswith("size="):
                            storage_info["size"] = part.replace("size=", "")
                            break
                parsed_config["disks"].append(storage_info)

            # Also add to general storage for backwards compatibility
            parsed_config["storage"].append(storage_info)

        # Hardware Devices
        elif key.startswith(("hostpci", "usb", "serial", "audio")):
            parsed_config["devices"].append({"device": key, "config": value})

        # Cloud-init Configuration
        elif key in [
            "ciuser",
            "cipassword",
            "sshkeys",
            "ipconfig0",
            "ipconfig1",
            "ipconfig2",
            "nameserver",
            "searchdomain",
            "cicustom",
        ]:
            if key == "sshkeys":
                # Parse SSH keys to extract meaningful information
                parsed_config["cloud_init"][key] = {
                    "raw": value,
                    "parsed": parse_ssh_keys(value),
                }
            else:
                parsed_config["cloud_init"][key] = value

        # General VM Settings
        elif key in [
            "name",
            "ostype",
            "boot",
            "bootdisk",
            "onboot",
            "startup",
            "protection",
            "template",
            "tags",
        ]:
            parsed_config["general"][key] = value

        # Everything else
        else:
            parsed_config["other"][key] = value

    # Add default CPU values if CPU section is empty (Proxmox defaults)
    if not parsed_config["cpu"]:
        parsed_config["cpu"] = {"sockets": 1, "cores": 1}
    else:
        # Ensure default values exist if not specified
        if "sockets" not in parsed_config["cpu"]:
            parsed_config["cpu"]["sockets"] = 1
        if "cores" not in parsed_config["cpu"]:
            parsed_config["cpu"]["cores"] = 1

    return parsed_config


def parse_ssh_keys(ssh_keys_string):
    """Parse SSH keys from URL-encoded string and extract key info"""
    if not ssh_keys_string:
        return []

    import urllib.parse

    # URL decode the string
    decoded_keys = urllib.parse.unquote(ssh_keys_string)

    # Split by newlines to get individual keys
    key_lines = [line.strip() for line in decoded_keys.split("\n") if line.strip()]

    parsed_keys = []
    for key_line in key_lines:
        if not key_line:
            continue

        # SSH key format: <type> <key-data> <comment>
        parts = key_line.split(" ", 2)
        if len(parts) >= 2:
            key_type = parts[0]  # ssh-rsa, ssh-ed25519, etc.
            key_data = parts[1]  # The actual key data
            comment = parts[2] if len(parts) > 2 else "no comment"

            # Truncate key data for display
            key_preview = (
                key_data[:10] + "..." + key_data[-10:]
                if len(key_data) > 20
                else key_data
            )

            parsed_keys.append(
                {
                    "type": key_type,
                    "preview": key_preview,
                    "comment": comment,
                    "full_key": key_line,
                }
            )
        else:
            # Fallback for malformed keys
            parsed_keys.append(
                {
                    "type": "unknown",
                    "preview": (
                        key_line[:30] + "..." if len(key_line) > 30 else key_line
                    ),
                    "comment": "malformed key",
                    "full_key": key_line,
                }
            )

    return parsed_keys


# ─── LXC Advanced Configuration Helpers ──────────────────────────────────────

LXC_FEATURE_FLAGS = [
    ("nesting", "Container nesting (Docker-in-LXC, systemd, nested LXC)"),
    ("keyctl", "Key retention service (required by some apps)"),
    ("fuse", "FUSE filesystem support"),
    ("mount", "Mount arbitrary filesystems (cifs, nfs, ext4…)"),
    ("mknod", "Allow mknod in unprivileged containers"),
]

_LXC_FLAG_ORDER = [name for name, _ in LXC_FEATURE_FLAGS]


def parse_lxc_features(features_str):
    """Parse LXC features string into a flag dict.

    'nesting=1,keyctl=1' → {'nesting': '1', 'keyctl': '1'}
    """
    if not features_str:
        return {}
    result = {}
    for part in str(features_str).split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
        else:
            result[part] = "1"
    return result


def serialize_lxc_features(flags_dict):
    """Serialize a flag dict to a PVE features string.

    Only enabled flags (value truthy/1) are included.
    Known flags are written in canonical order; unknown flags appended.
    """
    parts = []
    seen = set()
    for k in _LXC_FLAG_ORDER:
        if k in flags_dict:
            seen.add(k)
            if str(flags_dict[k]) in ("1", "true", "True"):
                parts.append(f"{k}=1")
    for k, v in flags_dict.items():
        if k not in seen and str(v) in ("1", "true", "True"):
            parts.append(f"{k}=1")
    return ",".join(parts)


def check_lxc_write_permission(proxmox, vmid, node=None):
    """Return True if the current connection can write to this container's config."""
    # root@pam and the Proxmox root user always have unconditional superuser access.
    if node:
        meta = connection_metadata.get(node) or {}
        user = meta.get("user", "")
        username = user.split("@")[0]  # strip realm: "root@pam" → "root"
        if user == "root@pam" or username == "root":
            return True
    try:
        perms = proxmox.access.permissions.get(path=f"/vms/{vmid}")
        # Empty dict: Proxmox returns {} for superusers with implicit access.
        if not perms:
            return True
        # Proxmox nests privileges under the path key:
        # {'/vms/210': {'VM.Config.Options': 1, ...}}
        path_key = f"/vms/{vmid}"
        privs = perms.get(path_key, perms)
        if not privs:
            return True
        write_privs = {
            "VM.Config.Options",
            "VM.Allocate",
            "VM.Config.Disk",
            "VM.Config.Network",
        }
        return any(privs.get(p) for p in write_privs)
    except Exception:
        return True  # Assume write access if permission check is unavailable


# ─── LXC Device & Mount Helpers ──────────────────────────────────────────────

# Known device cgroup info for PVE < 8.2 fallback (type, major, minor)
KNOWN_LXC_DEVICE_CGROUPS = {
    "/dev/dri/card0": ("c", 226, 0),
    "/dev/dri/card1": ("c", 226, 1),
    "/dev/dri/renderD128": ("c", 226, 128),
    "/dev/dri/renderD129": ("c", 226, 129),
    "/dev/net/tun": ("c", 10, 200),
    "/dev/fuse": ("c", 10, 229),
    "/dev/kvm": ("c", 10, 232),
}


def parse_lxc_dev(dev_str):
    """Parse a dev[n] config value → dict with path and optional uid/gid/mode."""
    if not dev_str:
        return {}
    parts = str(dev_str).split(",")
    result = {"path": parts[0].strip()}
    for part in parts[1:]:
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def serialize_lxc_dev(dev_dict):
    """Serialize a dev[n] dict → PVE config string."""
    parts = [dev_dict.get("path", "")]
    for k in ["uid", "gid", "mode"]:
        v = dev_dict.get(k)
        if v is not None and str(v) != "":
            parts.append(f"{k}={v}")
    return ",".join(p for p in parts if p)


def parse_lxc_mp(mp_str):
    """Parse an mp[n] config value → dict with host_path and options."""
    if not mp_str:
        return {}
    parts = str(mp_str).split(",")
    result = {"host_path": parts[0].strip()}
    for part in parts[1:]:
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def serialize_lxc_mp(mp_dict):
    """Serialize an mp[n] dict → PVE config string."""
    parts = [mp_dict.get("host_path", "")]
    for k in ["mp", "ro", "backup", "quota", "shared"]:
        v = mp_dict.get(k)
        if v is not None and str(v) != "":
            parts.append(f"{k}={v}")
    return ",".join(p for p in parts if p)


def is_lxc_bind_mount(mp_str):
    """True if mp[n] value is a host-path bind mount (starts with '/')."""
    return bool(mp_str) and str(mp_str).strip().startswith("/")


def get_pve_version_tuple(node):
    """Return PVE version as (major, minor, patch) for a node."""
    meta = connection_metadata.get(node) or {}
    ver_str = meta.get("pve_version", "")
    if not ver_str:
        return (0, 0, 0)
    try:
        clean = ver_str.replace("-", ".").split(".")
        nums = [int(x) for x in clean if x.isdigit()]
        return tuple((nums + [0, 0, 0])[:3])
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _next_key_index(config, prefix):
    """Return the lowest unused numeric index for a config key prefix (dev, mp, lxc…)."""
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    used = {int(m.group(1)) for k in config for m in [pattern.match(k)] if m}
    i = 0
    while i in used:
        i += 1
    return i


def _proxmox_error_response(e):
    """Convert a proxmoxer/requests exception to a JSON Flask response."""
    traceback.print_exc()
    err = str(e)
    if "403" in err or "Forbidden" in err:
        return jsonify({"error": err}), 403
    if "400" in err or "Bad Request" in err:
        return jsonify({"error": err}), 400
    if "404" in err or "Not Found" in err:
        return jsonify({"error": err}), 404
    return jsonify({"error": err}), 500


def _check_is_root(node):
    """Return True if the connection for this node authenticated as root@pam."""
    meta = connection_metadata.get(node) or {}
    user = meta.get("user", "")
    return user == "root@pam" or user.split("@")[0] == "root"


def _backup_lxc_config(node, vmid, config):
    """Snapshot current LXC config before any write."""
    lxc_config_backups[f"{node}:{vmid}"] = {
        "config": dict(config),
        "timestamp": datetime.now().isoformat(),
        "features": config.get("features", ""),
    }


def _restore_advanced_keys(proxmox, node, vmid, backup_config, current_config):
    """Restore all LXC advanced keys (features, dev[n], lxc[n], mp[n] binds) from backup."""
    adv_pattern = re.compile(r"^(dev|lxc)\d+$")

    def is_advanced(key, cfg):
        return (
            key == "features"
            or bool(adv_pattern.match(key))
            or (re.match(r"^mp\d+$", key) and is_lxc_bind_mount(cfg.get(key, "")))
        )

    backup_adv = {
        k: v for k, v in backup_config.items() if is_advanced(k, backup_config)
    }
    current_adv = {
        k: v for k, v in current_config.items() if is_advanced(k, current_config)
    }

    params = {}
    delete_list = [k for k in current_adv if k not in backup_adv]

    for k, v in backup_adv.items():
        if current_config.get(k) != v:
            params[k] = v

    if delete_list:
        params["delete"] = ",".join(delete_list)

    if params:
        proxmox.nodes(node).lxc(vmid).config.put(**params)

    return params, delete_list


# ─── End Device & Mount Helpers ───────────────────────────────────────────────

# ─── LXC idmap Helpers ───────────────────────────────────────────────────────


def parse_lxc_idmap_line(raw_value):
    """Parse a raw lxc.idmap value into a structured dict.

    Expected format (from lxc[n] raw key): 'lxc.idmap = u 0 100000 65536'
    Also accepts the bare mapping portion: 'u 0 100000 65536'
    Returns None if the value is not an idmap line.
    """
    raw = str(raw_value).strip()
    # Strip the 'lxc.idmap = ' prefix if present
    if "lxc.idmap" in raw:
        idx = raw.find("=")
        if idx == -1:
            return None
        raw = raw[idx + 1 :].strip()

    parts = raw.split()
    if len(parts) != 4 or parts[0] not in ("u", "g"):
        return None
    try:
        return {
            "type": parts[0],  # 'u' or 'g'
            "ct_id": int(parts[1]),  # start ID inside container
            "host_id": int(parts[2]),  # start ID on host
            "count": int(parts[3]),  # count of IDs mapped
        }
    except ValueError:
        return None


def serialize_lxc_idmap_line(idmap_dict):
    """Serialize an idmap dict to the raw lxc.idmap config value string."""
    return (
        f"lxc.idmap = {idmap_dict['type']} "
        f"{idmap_dict['ct_id']} {idmap_dict['host_id']} {idmap_dict['count']}"
    )


def collect_lxc_idmaps(config):
    """Return all lxc.idmap entries from a container config as a list of dicts.

    Each dict has 'key' (the lxc[n] config key), 'raw', and parsed fields.
    """
    result = []
    lxc_pattern = re.compile(r"^lxc(\d+)$")
    for key in sorted(
        config, key=lambda k: int(k[3:]) if lxc_pattern.match(k) else 999
    ):
        if not lxc_pattern.match(key):
            continue
        raw = str(config[key])
        if "lxc.idmap" not in raw:
            continue
        parsed = parse_lxc_idmap_line(raw)
        if parsed:
            parsed["key"] = key
            parsed["raw"] = raw
            result.append(parsed)
    return result


# ─── End LXC idmap Helpers ───────────────────────────────────────────────────

# ─── LXC Profile Helpers ─────────────────────────────────────────────────────


def compute_profile_diff(profile, config):
    """Compute what a profile would add to an existing container config.

    Returns a dict with:
    - features_to_add: list of 'flag=1' strings not yet enabled
    - features_already_set: list already enabled
    - devices_to_add: list of device dicts not yet in config
    - devices_already_present: list already matching a dev[n] or lxc[n] entry
    - changes_needed: True if any addition is required
    - devices_require_stop: True if new devices would be added
    """
    current_features = parse_lxc_features(config.get("features", ""))
    profile_features = profile.get("features", {})

    features_to_add = []
    features_already_set = []
    for flag, val in profile_features.items():
        if str(val) in ("1", "true", "True"):
            if current_features.get(flag) == "1":
                features_already_set.append(f"{flag}=1")
            else:
                features_to_add.append(f"{flag}=1")

    # Collect existing device paths from dev[n] and lxc.mount.entry
    existing_paths = set()
    dev_pattern = re.compile(r"^dev\d+$")
    lxc_pattern = re.compile(r"^lxc\d+$")
    for key, val in config.items():
        if dev_pattern.match(key):
            parsed = parse_lxc_dev(val)
            if parsed.get("path"):
                existing_paths.add(parsed["path"])
        elif lxc_pattern.match(key) and "lxc.mount.entry" in str(val):
            # Extract path from: lxc.mount.entry = /dev/... dev/... none ...
            parts = str(val).split()
            if len(parts) >= 4:
                existing_paths.add(parts[2])  # source path

    devices_to_add = []
    devices_already_present = []
    for dev in profile.get("devices", []):
        if dev.get("path") in existing_paths:
            devices_already_present.append(dev)
        else:
            devices_to_add.append(dev)

    return {
        "features_to_add": features_to_add,
        "features_already_set": features_already_set,
        "devices_to_add": devices_to_add,
        "devices_already_present": devices_already_present,
        "changes_needed": bool(features_to_add or devices_to_add),
        "devices_require_stop": bool(devices_to_add),
    }


# ─── End LXC Profile Helpers ─────────────────────────────────────────────────

# ─── End LXC helpers ──────────────────────────────────────────────────────────

# ROUTES - Make sure all routes are defined


@app.route("/api/clusters")
def api_clusters():
    """API endpoint to get all available clusters"""
    clusters_info = []
    for cluster_id, cluster_config in all_clusters.items():
        clusters_info.append(
            {
                "id": cluster_id,
                "name": cluster_config.get("name", cluster_id),
                "active": cluster_id == current_cluster_id,
            }
        )
    return jsonify({"clusters": clusters_info, "current": current_cluster_id})


@app.route("/api/switch-cluster/<cluster_id>", methods=["POST"])
def api_switch_cluster(cluster_id):
    """API endpoint to switch to a different cluster"""

    if cluster_id not in all_clusters:
        return jsonify({"error": "Cluster not found"}), 404

    try:
        success = init_proxmox_connections(cluster_id)
        if success:
            return jsonify(
                {
                    "success": True,
                    "cluster": {
                        "id": current_cluster_id,
                        "name": all_clusters[current_cluster_id].get(
                            "name", current_cluster_id
                        ),
                    },
                }
            )
        else:
            return jsonify({"error": "Failed to connect to cluster nodes"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Template context processor to make cluster info available in all templates
@app.context_processor
def inject_cluster_info():
    return {
        "current_cluster": {
            "id": current_cluster_id,
            "name": (
                all_clusters.get(current_cluster_id, {}).get("name", current_cluster_id)
                if current_cluster_id
                else "No Cluster"
            ),
        },
        "all_clusters": [
            {"id": cluster_id, "name": cluster_config.get("name", cluster_id)}
            for cluster_id, cluster_config in all_clusters.items()
        ],
        "demo_mode": DEMO_MODE,
        "demo_message": DEMO_MESSAGE,
        "app_version": APP_VERSION,
    }


@app.route("/connect", methods=["GET", "POST"])
def connect():
    """Connection setup page"""
    global config, config_file_exists

    if request.method == "GET":
        # Pass existing clusters to template
        existing_clusters = []
        for cluster_id, cluster_config in all_clusters.items():
            existing_clusters.append(
                {
                    "id": cluster_id,
                    "name": cluster_config.get("name", cluster_id),
                    "nodes": cluster_config.get("nodes", []),
                }
            )

        return render_template("connect.html", existing_clusters=existing_clusters)

    # Handle form submission

    if DEMO_MODE:
        flash("Cluster configuration is disabled in demo mode.", "error")
        return render_template(
            "connect.html",
            existing_clusters=[
                {"id": cid, "name": cfg.get("name", cid), "nodes": cfg.get("nodes", [])}
                for cid, cfg in all_clusters.items()
            ],
        )

    try:
        cluster_name = request.form.get("cluster_name")
        cluster_id = request.form.get("cluster_id")
        node_host = request.form.get("node_host")
        node_user = request.form.get("node_user")
        auth_type = request.form.get("auth_type", "password")
        node_password = request.form.get("node_password")
        token_name = request.form.get("token_name")
        token_value = request.form.get("token_value")
        verify_ssl = "verify_ssl" in request.form
        incremental_vmid = "incremental_vmid" in request.form

        # Check if cluster ID already exists
        if cluster_id in all_clusters:
            flash(
                f'Cluster ID "{cluster_id}" already exists. Please choose a different ID.',
                "error",
            )
            return render_template(
                "connect.html",
                existing_clusters=[
                    {
                        "id": cid,
                        "name": cfg.get("name", cid),
                        "nodes": cfg.get("nodes", []),
                    }
                    for cid, cfg in all_clusters.items()
                ],
            )

        # Create node config based on auth type
        node_config = {
            "host": node_host,
            "user": node_user,
            "verify_ssl": verify_ssl,
        }

        if auth_type == "token":
            if not token_name or not token_value:
                flash(
                    "Token name and token value are required for API token authentication.",
                    "error",
                )
                return render_template("connect.html")
            node_config["token_name"] = token_name
            node_config["token_value"] = token_value
        else:
            if not node_password:
                flash("Password is required for password authentication.", "error")
                return render_template("connect.html")
            node_config["password"] = node_password

        # Create new cluster config
        new_cluster = {
            "id": cluster_id,
            "name": cluster_name,
            "incremental_vmid": incremental_vmid,
            "nodes": [node_config],
        }

        # Add to existing config or create new
        if config.get("clusters"):
            config["clusters"].append(new_cluster)
        else:
            config = dict(config)  # Explicit assignment for linter
            config["clusters"] = [new_cluster]

        # Test connection first
        try:
            test_proxmox = create_proxmox_connection(node_config)
            version = test_proxmox.version.get()
            print(f"Test connection successful - PVE {version['version']}")
        except Exception as e:
            flash(f"Connection test failed: {str(e)}", "error")
            return render_template("connect.html")

        # Save config file
        save_config_file(config)

        # Reload configuration
        config_file_exists = True

        # Reinitialize clusters
        init_all_clusters()

        # Switch to the new cluster if no current cluster or it's the first one
        if not current_cluster_id or len(all_clusters) == 1:
            init_proxmox_connections(cluster_id)

        flash(f'Successfully added cluster "{cluster_name}"!', "success")
        return redirect(url_for("connect"))

    except Exception as e:
        flash(f"Error saving configuration: {str(e)}", "error")
        return render_template("connect.html")


@app.route("/api/test-connection", methods=["POST"])
def api_test_connection():
    """Test connection to Proxmox without saving config"""
    try:
        node_host = request.form.get("node_host")
        node_user = request.form.get("node_user")
        auth_type = request.form.get("auth_type", "password")
        node_password = request.form.get("node_password")
        token_name = request.form.get("token_name")
        token_value = request.form.get("token_value")
        verify_ssl = "verify_ssl" in request.form

        # Build node config for connection helper
        node_config = {
            "host": node_host,
            "user": node_user,
            "verify_ssl": verify_ssl,
        }

        if auth_type == "token":
            node_config["token_name"] = token_name
            node_config["token_value"] = token_value
        else:
            node_config["password"] = node_password

        # Test connection
        test_proxmox = create_proxmox_connection(node_config)

        version = test_proxmox.version.get()
        nodes = test_proxmox.nodes.get()

        # Also test permissions to read cluster resources
        permission_warning = None
        try:
            test_proxmox.cluster.resources.get(type="vm")
        except Exception as perm_error:
            error_str = str(perm_error).lower()
            if "permission" in error_str or "403" in error_str:
                permission_warning = (
                    "Connection successful but token may lack permissions to read resources. "
                    "If using API tokens, ensure 'Privilege Separation' is unchecked or "
                    "assign appropriate permissions (e.g., PVEAuditor role) to the token."
                )

        response = {
            "success": True,
            "version": version["version"],
            "node_name": nodes[0]["node"] if nodes else "Unknown",
            "nodes_count": len(nodes),
        }
        if permission_warning:
            response["warning"] = permission_warning

        return jsonify(response)

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/delete-cluster/<cluster_id>", methods=["DELETE"])
def api_delete_cluster(cluster_id):
    """Delete a cluster configuration"""
    global config

    if DEMO_MODE:
        return (
            jsonify({"error": "Cluster configuration is disabled in demo mode."}),
            403,
        )

    try:
        if cluster_id not in all_clusters:
            return jsonify({"error": "Cluster not found"}), 404

        # Don't allow deleting the last cluster
        if len(all_clusters) <= 1:
            return jsonify({"error": "Cannot delete the last cluster"}), 400

        # Remove cluster from config
        config = dict(config)  # Explicit assignment for linter
        config["clusters"] = [
            c for c in config["clusters"] if c.get("id") != cluster_id
        ]

        # Save config file
        save_config_file(config)

        # If we deleted the current cluster, switch to another one
        if current_cluster_id == cluster_id:
            init_all_clusters()
            remaining_clusters = list(all_clusters.keys())
            if remaining_clusters:
                init_proxmox_connections(remaining_clusters[0])
        else:
            # Just reload cluster list
            init_all_clusters()

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cluster/<cluster_id>", methods=["GET"])
def api_get_cluster(cluster_id):
    """Get cluster configuration (credentials masked)"""
    try:
        if cluster_id not in all_clusters:
            return jsonify({"error": "Cluster not found"}), 404

        cluster_config = all_clusters[cluster_id]

        # Build response with masked credentials
        nodes = []
        for node in cluster_config.get("nodes", []):
            node_info = {
                "host": node.get("host", ""),
                "user": node.get("user", ""),
                "verify_ssl": node.get("verify_ssl", True),
            }
            # Indicate auth type without exposing credentials
            if node.get("token_name"):
                node_info["auth_type"] = "token"
                node_info["token_name"] = node.get("token_name", "")
                node_info["has_token_value"] = bool(node.get("token_value"))
            else:
                node_info["auth_type"] = "password"
                node_info["has_password"] = bool(node.get("password"))
            nodes.append(node_info)

        return jsonify(
            {
                "success": True,
                "cluster": {
                    "id": cluster_id,
                    "name": cluster_config.get("name", cluster_id),
                    "incremental_vmid": cluster_config.get("incremental_vmid", False),
                    "nodes": nodes,
                },
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cluster/<cluster_id>", methods=["PUT"])
def api_update_cluster(cluster_id):
    """Update cluster configuration"""
    global config

    if DEMO_MODE:
        return (
            jsonify({"error": "Cluster configuration is disabled in demo mode."}),
            403,
        )

    try:
        if cluster_id not in all_clusters:
            return jsonify({"error": "Cluster not found"}), 404

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        # Find and update cluster in config
        for i, cluster in enumerate(config["clusters"]):
            if cluster.get("id") == cluster_id:
                # Update cluster name if provided
                if "name" in data:
                    config["clusters"][i]["name"] = data["name"]

                # Update incremental_vmid setting if provided
                if "incremental_vmid" in data:
                    config["clusters"][i]["incremental_vmid"] = data["incremental_vmid"]

                # Update nodes if provided
                if "nodes" in data and len(data["nodes"]) > 0:
                    node_data = data["nodes"][0]  # Currently support single node
                    existing_node = (
                        config["clusters"][i]["nodes"][0]
                        if config["clusters"][i].get("nodes")
                        else {}
                    )

                    updated_node = {
                        "host": node_data.get("host", existing_node.get("host", "")),
                        "user": node_data.get("user", existing_node.get("user", "")),
                        "verify_ssl": node_data.get(
                            "verify_ssl", existing_node.get("verify_ssl", True)
                        ),
                    }

                    # Handle auth type - ensure we don't mix password and token auth
                    auth_type = node_data.get("auth_type", "password")
                    if auth_type == "token":
                        # Token auth - do NOT include password field
                        token_name = node_data.get("token_name")
                        token_value = node_data.get("token_value")

                        if token_name:
                            updated_node["token_name"] = token_name
                        elif existing_node.get("token_name"):
                            updated_node["token_name"] = existing_node["token_name"]

                        if token_value:
                            updated_node["token_value"] = token_value
                        elif existing_node.get("token_value"):
                            updated_node["token_value"] = existing_node["token_value"]

                        # Validate that we have both token fields
                        if not updated_node.get("token_name") or not updated_node.get(
                            "token_value"
                        ):
                            return (
                                jsonify(
                                    {
                                        "error": "Both token name and token value are required for API token authentication"
                                    }
                                ),
                                400,
                            )
                    else:
                        # Password auth - do NOT include token fields
                        password = node_data.get("password")
                        if password:
                            updated_node["password"] = password
                        elif existing_node.get("password"):
                            updated_node["password"] = existing_node["password"]

                        # Validate that we have password
                        if not updated_node.get("password"):
                            return (
                                jsonify(
                                    {
                                        "error": "Password is required for password authentication"
                                    }
                                ),
                                400,
                            )

                    config["clusters"][i]["nodes"] = [updated_node]

                break

        # Test connection before saving
        if "nodes" in data and data.get("test_connection", True):
            try:
                test_node = config["clusters"][i]["nodes"][0]
                test_proxmox = create_proxmox_connection(test_node)
                # Basic connectivity test
                test_proxmox.version.get()
                # Also test that we can read cluster resources (catches permission issues)
                try:
                    test_proxmox.cluster.resources.get(type="vm")
                except Exception as perm_error:
                    error_str = str(perm_error).lower()
                    if "permission" in error_str or "403" in error_str:
                        return (
                            jsonify(
                                {
                                    "error": "Connection successful but token lacks permissions to read resources. "
                                    "If using API tokens, ensure 'Privilege Separation' is unchecked or "
                                    "assign appropriate permissions (e.g., PVEAuditor role) to the token."
                                }
                            ),
                            400,
                        )
                    raise
            except Exception as e:
                return jsonify({"error": f"Connection test failed: {str(e)}"}), 400

        # Save config file
        save_config_file(config)

        # Reinitialize clusters
        init_all_clusters()

        # Reinitialize connections if this is the current cluster
        if current_cluster_id == cluster_id:
            init_proxmox_connections(cluster_id)

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cluster-status/<cluster_id>")
def api_cluster_status(cluster_id):
    """Check if cluster is reachable"""
    try:
        if cluster_id not in all_clusters:
            return jsonify({"success": False, "error": "Cluster not found"})

        cluster_config = all_clusters[cluster_id]
        nodes = cluster_config.get("nodes", [])

        if not nodes:
            return jsonify({"success": False, "error": "No nodes configured"})

        # Test connection to first node
        node = nodes[0]
        test_proxmox = create_proxmox_connection(node)

        # Simple version check to test connectivity
        version = test_proxmox.version.get()

        return jsonify(
            {
                "success": True,
                "version": version.get("version", "Unknown"),
                "node_count": len(nodes),
            }
        )

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# Redirect to connect page if no config
@app.before_request
def check_config():
    # Skip check for connect-related routes and static files
    if request.endpoint in [
        "connect",
        "api_test_connection",
        "api_delete_cluster",
        "api_cluster_status",
        "static",
    ]:
        return

    # Redirect to connect page if no valid config
    if not config_file_exists or not all_clusters:
        return redirect(url_for("connect"))


@app.route("/")
def index():
    """Dashboard with overview"""
    resources = get_all_vms_and_containers()

    # Separate templates from regular VMs/containers
    templates = [r for r in resources if r.get("template")]
    non_templates = [r for r in resources if not r.get("template")]
    running_resources = [r for r in non_templates if r.get("status") == "running"]

    # Calculate statistics (excluding templates)
    total_vms = len([r for r in non_templates if r["type"] == "qemu"])
    total_containers = len([r for r in non_templates if r["type"] == "lxc"])
    total_templates = len(templates)
    running = len(running_resources)
    stopped = len([r for r in non_templates if r.get("status") == "stopped"])

    # Find top resource consumers (only running VMs/containers)
    top_cpu = sorted(running_resources, key=lambda x: x.get("cpu", 0), reverse=True)[:5]
    top_memory = sorted(running_resources, key=lambda x: x.get("mem", 0), reverse=True)[
        :5
    ]

    # Get node list for metrics
    nodes = [n["name"] for n in cluster_nodes if n.get("status") == "online"]
    total_nodes = len(cluster_nodes)
    online_nodes = len(nodes)

    return render_template(
        "index.html",
        total_vms=total_vms,
        total_containers=total_containers,
        total_templates=total_templates,
        running=running,
        stopped=stopped,
        top_cpu=top_cpu,
        top_memory=top_memory,
        nodes=nodes,
        total_nodes=total_nodes,
        online_nodes=online_nodes,
    )


@app.route("/vms")
def vms():
    """List all VMs and containers"""
    resources = get_all_vms_and_containers()

    # Sort by CPU usage by default
    sort_by = request.args.get("sort", "cpu")
    reverse = request.args.get("order", "desc") == "desc"

    if sort_by == "cpu":
        resources.sort(key=lambda x: x.get("cpu", 0), reverse=reverse)
    elif sort_by == "memory":
        resources.sort(key=lambda x: x.get("mem", 0), reverse=reverse)
    elif sort_by == "disk":
        resources.sort(key=lambda x: x.get("disk", 0), reverse=reverse)
    elif sort_by == "name":
        resources.sort(key=lambda x: x.get("name", ""), reverse=reverse)

    return render_template("vms.html", resources=resources)


@app.route("/vm/<node>/<vmid>")
def vm_detail(node, vmid):
    """Show detailed VM information"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        flash("Node connection not found", "error")
        return redirect(url_for("vms"))

    try:
        # Get VM config
        vm_type = "qemu"  # Default to qemu
        try:
            config = proxmox.nodes(node).qemu(vmid).config.get()
        except:
            vm_type = "lxc"
            config = proxmox.nodes(node).lxc(vmid).config.get()

        # Get current status
        if vm_type == "qemu":
            status = proxmox.nodes(node).qemu(vmid).status.current.get()
        else:
            status = proxmox.nodes(node).lxc(vmid).status.current.get()

        # Get available nodes for migration (all cluster nodes except current)
        available_nodes = sorted(
            [
                n["name"]
                for n in cluster_nodes
                if n["name"] != node and n.get("status") == "online"
            ]
        )

        # Get guest agent disk information for QEMU VMs — only when the agent is
        # actually enabled, otherwise the agent call stalls the whole page load.
        guest_disk_info = None
        if (
            vm_type == "qemu"
            and status.get("status") == "running"
            and is_agent_enabled(config)
        ):
            guest_disk_info = get_qemu_guest_disk_info(proxmox, node, vmid)
        elif vm_type == "lxc" and status.get("status") == "running":
            # Containers have no guest agent; the host reports rootfs usage
            # directly via status/current (same as 'pct df' rootfs row).
            guest_disk_info = get_lxc_disk_info(status)

        # Check for active migration tasks
        migration_info = None
        try:
            all_tasks = proxmox.nodes(node).tasks.get()
            for task in all_tasks:
                task_id = task.get("id", "")
                task_type = task.get("type", "")
                task_status = task.get("status", "").lower()

                # Check if this is an active migration task for this VM/container
                if (
                    task_type in ["qmigrate", "vzmigrate"]
                    and vmid in str(task_id)
                    and "running" in task_status
                ):

                    migration_info = {
                        "status": task.get("status"),
                        "target": task.get("target"),
                        "start_time": (
                            datetime.fromtimestamp(task.get("starttime")).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            if task.get("starttime")
                            else "Unknown"
                        ),
                        "task_id": task.get("upid"),
                    }
                    break
        except Exception as e:
            print(f"Error checking migration status: {e}")

        # Parse configuration into structured groups
        parsed_config = parse_vm_configuration(config, vm_type)

        return render_template(
            "vm_detail.html",
            vm_type=vm_type,
            vmid=vmid,
            node=node,
            config=config,
            parsed_config=parsed_config,
            status=status,
            available_nodes=available_nodes,
            migration_info=migration_info,
            guest_disk_info=guest_disk_info,
        )
    except Exception as e:
        flash(f"Error getting VM details: {e}", "error")
        return redirect(url_for("vms"))


@app.route("/vm/<node>/<vmid>/edit")
def vm_edit(node, vmid):
    """Show VM/Container configuration edit page"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        flash("Node connection not found", "error")
        return redirect(url_for("vms"))

    try:
        # Determine VM type and get info/config
        vm_type = "qemu"
        try:
            vm_info = proxmox.nodes(node).qemu(vmid).status.current.get()
            config = proxmox.nodes(node).qemu(vmid).config.get()
        except:
            vm_type = "lxc"
            vm_info = proxmox.nodes(node).lxc(vmid).status.current.get()
            config = proxmox.nodes(node).lxc(vmid).config.get()

        ci_sshkeys = unquote(config.get("sshkeys", "")) if config.get("sshkeys") else ""
        return render_template(
            "vm_edit.html",
            node=node,
            vm=vm_info,
            config=config,
            vm_type=vm_type,
            ci_sshkeys=ci_sshkeys,
        )
    except Exception as e:
        flash(f"Error loading VM/Container configuration: {e}", "error")
        return redirect(url_for("vm_detail", node=node, vmid=vmid))


@app.route("/vm/<node>/<vmid>/clone")
def vm_clone(node, vmid):
    """Show VM clone page"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        flash("Node connection not found", "error")
        return redirect(url_for("vms"))

    try:
        # Get VM info and config
        vm_info = proxmox.nodes(node).qemu(vmid).status.current.get()
        config = proxmox.nodes(node).qemu(vmid).config.get()

        # Get available nodes for cloning
        available_nodes = sorted(
            [n["name"] for n in cluster_nodes if n.get("status") == "online"]
        )

        return render_template(
            "vm_clone.html",
            node=node,
            vmid=vmid,
            vm=vm_info,
            config=config,
            available_nodes=available_nodes,
        )
    except Exception as e:
        flash(f"Error loading VM information: {e}", "error")
        return redirect(url_for("vms"))


@app.route("/vm/<node>/<vmid>/<action>", methods=["POST"])
def vm_action(node, vmid, action):
    """Perform action on VM"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node connection not found"}), 404

    try:
        # Determine VM type
        vm_type = "qemu"
        try:
            proxmox.nodes(node).qemu(vmid).config.get()
        except:
            vm_type = "lxc"

        # Get VM object
        if vm_type == "qemu":
            vm = proxmox.nodes(node).qemu(vmid)
        else:
            vm = proxmox.nodes(node).lxc(vmid)

        # Perform action
        if action == "start":
            vm.status.start.post()
            flash(f"VM {vmid} started successfully", "success")
        elif action == "stop":
            vm.status.stop.post()
            flash(f"VM {vmid} stopped successfully", "success")
        elif action == "shutdown":
            vm.status.shutdown.post()
            flash(f"VM {vmid} shutdown initiated", "success")
        elif action == "reset":
            vm.status.reset.post()
            flash(f"VM {vmid} reset successfully", "success")
        elif action == "migrate":
            target_node = request.form.get("target_node")
            target_storage = request.form.get("target_storage")

            # Check if migration is already in progress
            try:
                all_tasks = proxmox.nodes(node).tasks.get()
                for task in all_tasks:
                    task_id = task.get("id", "")
                    task_type = task.get("type", "")
                    task_status = task.get("status", "").lower()

                    if (
                        task_type in ["qmigrate", "vzmigrate"]
                        and vmid in str(task_id)
                        and "running" in task_status
                    ):
                        flash(
                            "Migration already in progress. Please wait for current migration to complete.",
                            "warning",
                        )
                        return redirect(url_for("vm_detail", node=node, vmid=vmid))
            except Exception as e:
                print(f"Error checking migration status: {e}")

            if target_node:
                vm_type_name = "VM" if vm_type == "qemu" else "Container"

                if target_storage:
                    # Two-step process: migrate VM/container, then move storage
                    try:
                        # Step 1: Migrate the VM/container with local disks
                        if vm_type == "qemu":
                            # VMs can use live migration
                            migrate_params = {
                                "target": target_node,
                                "online": 1,
                                "with-local-disks": 1,
                            }
                        else:
                            # LXC containers must use offline migration with restart
                            migrate_params = {
                                "target": target_node,
                                "with-local-disks": 1,
                                "restart": 1,
                            }

                        vm.migrate.post(**migrate_params)

                        migration_type = "live" if vm_type == "qemu" else "offline"
                        flash(
                            f"{vm_type_name} {vmid} {migration_type} migration to {target_node} started (storage will remain on current storage)",
                            "success",
                        )

                    except Exception as migrate_error:
                        # If migration with storage fails, try without storage migration
                        try:
                            if vm_type == "qemu":
                                simple_params = {"target": target_node, "online": 1}
                            else:
                                simple_params = {"target": target_node, "restart": 1}

                            vm.migrate.post(**simple_params)
                            migration_type = "live" if vm_type == "qemu" else "offline"
                            flash(
                                f"{vm_type_name} {vmid} {migration_type} migration to {target_node} started (storage migration not supported)",
                                "warning",
                            )
                        except Exception as simple_error:
                            raise migrate_error
                else:
                    # Simple migration without storage
                    if vm_type == "qemu":
                        # VMs can use live migration
                        migrate_params = {"target": target_node, "online": 1}
                    else:
                        # LXC containers must use offline migration with restart
                        migrate_params = {"target": target_node, "restart": 1}

                    vm.migrate.post(**migrate_params)
                    migration_type = "live" if vm_type == "qemu" else "offline"
                    flash(
                        f"{vm_type_name} {vmid} {migration_type} migration to {target_node} started",
                        "success",
                    )
            else:
                flash("Target node not specified", "error")
        else:
            flash(f"Unknown action: {action}", "error")

    except Exception as e:
        flash(f"Error performing action: {e}", "error")

    return redirect(url_for("vm_detail", node=node, vmid=vmid))


@app.route("/tasks")
def tasks():
    """Show all recent tasks from the cluster"""
    return render_template("tasks.html")


@app.route("/create", methods=["GET", "POST"])
def create_vm():
    """Create new VM or container"""
    if request.method == "POST":
        try:
            node = request.form.get("node")
            vm_type = request.form.get("type")
            creation_method = request.form.get("creation_method")
            name = request.form.get("name")
            vmid = request.form.get("vmid")
            storage = request.form.get("storage")
            disk_size = request.form.get("disk_size")
            bridge = request.form.get(
                "bridge", "vmbr0"
            )  # Default to vmbr0 if not specified

            proxmox = get_proxmox_connection(node, auto_renew=True)
            if not proxmox:
                flash("Invalid node selected", "error")
                return redirect(url_for("create_vm"))

            if creation_method == "template" and vm_type == "qemu":
                # Clone from VM template
                template_vmid = request.form.get("template")
                if not template_vmid:
                    flash("Template must be selected", "error")
                    return redirect(url_for("create_vm"))

                # Clone the VM
                upid = (
                    proxmox.nodes(node)
                    .qemu(template_vmid)
                    .clone.post(newid=vmid, name=name, full=1)  # Full clone
                )
                # Update next-id if incremental VMID is enabled
                update_cluster_next_id(proxmox, vmid)
                # Apply the guest-agent toggle after the clone finishes (default
                # off — clones inherit the template's agent setting).
                agent_enabled = request.form.get("agent") in ("1", "on", "true", "yes")
                spawn_agent_after_clone(node, node, vmid, upid, agent_enabled)
                flash(f"VM {name} cloned from template successfully", "success")

            elif vm_type == "qemu":
                # Create VM from scratch
                params = {
                    "vmid": vmid,
                    "name": name,
                    "memory": int(request.form.get("memory", 512)),
                    "cores": int(request.form.get("cores", 1)),
                    "sockets": int(request.form.get("sockets", 1)),
                    "cpu": "host",
                    "net0": f"virtio,bridge={bridge}",
                    "boot": "c",
                    "scsihw": "virtio-scsi-pci",
                }

                # Add disk if storage is specified
                if storage:
                    params["scsi0"] = f"{storage}:{disk_size}"
                    params["bootdisk"] = "scsi0"

                # Add ISO image if specified
                iso_image = request.form.get("iso")
                if iso_image:
                    params["ide2"] = f"{iso_image},media=cdrom"
                    # Update boot order to include CD-ROM first for OS installation
                    params["boot"] = "dc"  # CD-ROM first, then disk

                # QEMU guest agent — off by default (avoids slow detail-page loads
                # when the agent isn't installed in the guest).
                if request.form.get("agent") in ("1", "on", "true", "yes"):
                    params["agent"] = "1"

                # Create the VM
                proxmox.nodes(node).qemu.create(**params)
                # Update next-id if incremental VMID is enabled
                update_cluster_next_id(proxmox, vmid)

            elif vm_type == "lxc":
                # Create container
                ostemplate = request.form.get("ostemplate")
                if not ostemplate and creation_method != "blank":
                    flash("OS Template must be selected", "error")
                    return redirect(url_for("create_vm"))

                params = {
                    "vmid": vmid,
                    "hostname": name,
                    "memory": int(request.form.get("memory", 512)),
                    "cores": int(request.form.get("cores", 1)),
                    "net0": f"name=eth0,bridge={bridge},ip=dhcp",
                }

                # Add storage
                if storage:
                    params["rootfs"] = f"{storage}:{disk_size}"

                # Add template if specified
                if ostemplate:
                    params["ostemplate"] = ostemplate

                # Create the container
                proxmox.nodes(node).lxc.create(**params)
                # Update next-id if incremental VMID is enabled
                update_cluster_next_id(proxmox, vmid)

            flash(f"{vm_type.upper()} {name} created successfully", "success")
            return redirect(url_for("vms"))

        except Exception as e:
            flash(f"Error creating VM: {e}", "error")

    # Get all online nodes for the form
    nodes = sorted([n["name"] for n in cluster_nodes if n.get("status") == "online"])

    return render_template("create_vm.html", nodes=nodes)


@app.route("/storages")
def storages():
    """List all storage"""
    all_storages = []
    processed_storages = set()  # Track processed storages to avoid duplicates

    # Try to get storage info from cluster endpoint first
    if proxmox_nodes:
        try:
            node_name = next(iter(proxmox_nodes.keys()))
            proxmox = get_proxmox_connection(node_name, auto_renew=True)

            if not proxmox:
                return get_storages_fallback()

            storages = proxmox.cluster.resources.get(type="storage")

            for storage in storages:
                storage_key = f"{storage['node']}-{storage['storage']}"
                if storage_key not in processed_storages:
                    processed_storages.add(storage_key)
                    if storage.get("maxdisk") and storage.get("maxdisk") > 0:
                        storage["used_percent"] = (
                            storage.get("disk", 0) / storage["maxdisk"]
                        ) * 100
                    else:
                        storage["used_percent"] = 0
                    all_storages.append(storage)

        except Exception as e:
            print(f"Error getting cluster storage: {e}")
            # Fallback to node-by-node
            all_storages = get_storages_fallback()

    # Group shared storage by storage name
    grouped_storages = group_shared_storages(all_storages)

    # Sort storages alphabetically by storage name
    grouped_storages.sort(key=lambda x: x.get("storage", "").lower())

    return render_template("storages.html", storages=grouped_storages)


def get_storages_fallback():
    """Fallback method to get storage node by node"""
    all_storages = []
    processed_storages = set()

    for node_info in cluster_nodes:
        try:
            node_name = node_info["name"]
            proxmox = get_proxmox_connection(node_name, auto_renew=True)

            if not proxmox:
                print(f"No valid connection for node {node_name}")
                continue

            storages = proxmox.nodes(node_name).storage.get()
            for storage in storages:
                storage_key = f"{node_name}-{storage['storage']}"
                if storage_key not in processed_storages:
                    processed_storages.add(storage_key)
                    storage["node"] = node_name
                    if storage.get("total") and storage.get("total") > 0:
                        storage["used_percent"] = (
                            storage.get("used", 0) / storage["total"]
                        ) * 100
                    else:
                        storage["used_percent"] = 0
                    all_storages.append(storage)
        except Exception as e:
            print(f"Error getting storage from node {node_info['name']}: {e}")

    return all_storages


def group_shared_storages(all_storages):
    """Group storage by storage name and combine nodes only if shared=1"""
    storage_groups = defaultdict(list)

    # Group storages by storage name
    for storage in all_storages:
        storage_name = storage.get("storage", "")
        storage_groups[storage_name].append(storage)

    grouped_storages = []

    for storage_name, storage_list in storage_groups.items():
        if not storage_list:
            continue

        # Check if this storage is shared (has shared=1 flag)
        is_shared = any(storage.get("shared", 0) == 1 for storage in storage_list)

        if is_shared and len(storage_list) > 1:
            # Combine shared storage across multiple nodes
            base_storage = storage_list[0].copy()
            nodes = []
            max_disk = 0
            max_maxdisk = 0
            enabled_count = 0

            for storage in storage_list:
                nodes.append(storage.get("node", ""))
                # For shared storage, use max values instead of sum (same storage seen from different nodes)
                if storage.get("disk"):
                    max_disk = max(max_disk, storage.get("disk", 0))
                if storage.get("maxdisk"):
                    max_maxdisk = max(max_maxdisk, storage.get("maxdisk", 0))
                if (
                    storage.get("enabled", 0) == 1
                    or storage.get("status") == "available"
                ):
                    enabled_count += 1

            # Update the base storage with combined data
            base_storage["nodes"] = nodes
            base_storage["node_count"] = len(nodes)
            base_storage["disk"] = max_disk
            base_storage["maxdisk"] = max_maxdisk

            # Calculate usage percentage
            if max_maxdisk > 0:
                base_storage["used_percent"] = (max_disk / max_maxdisk) * 100
            else:
                base_storage["used_percent"] = 0

            # Storage is considered active if it's enabled on any node
            base_storage["is_active"] = enabled_count > 0

            grouped_storages.append(base_storage)
        else:
            # Add each storage separately (not shared or only on one node)
            for storage in storage_list:
                # Ensure individual storage has proper is_active flag
                storage["is_active"] = (
                    storage.get("enabled", 0) == 1
                    or storage.get("status") == "available"
                )
                grouped_storages.append(storage)

    return grouped_storages


@app.route("/networks")
def networks():
    """List all networks"""
    all_networks = []
    processed_networks = set()

    for node_info in cluster_nodes:
        try:
            node_name = node_info["name"]
            proxmox = node_info["connection"]

            networks = proxmox.nodes(node_name).network.get()
            for network in networks:
                network_key = f"{node_name}-{network['iface']}"
                if network_key not in processed_networks:
                    processed_networks.add(network_key)
                    network["node"] = node_name
                    all_networks.append(network)
        except Exception as e:
            print(f"Error getting networks from node {node_info['name']}: {e}")

    return render_template("networks.html", networks=all_networks)


@app.route("/isos-templates")
def isos_templates():
    """Show ISOs and Templates management page"""
    return render_template("isos_templates.html")


@app.route("/cluster")
def cluster():
    """Show cluster information"""
    # Get current cluster info
    current_cluster_config = all_clusters.get(current_cluster_id, {})
    cluster_info = {
        "id": current_cluster_id,
        "name": current_cluster_config.get(
            "name", current_cluster_id or "Unknown Cluster"
        ),
        "nodes": [],
    }

    # Get detailed status for each node
    for node_info in cluster_nodes:
        try:
            node_name = node_info["name"]
            proxmox = node_info["connection"]

            # Get node status
            status = proxmox.nodes(node_name).status.get()
            status["host"] = node_name
            status["online"] = True

            # Get additional node information
            try:
                # Get node subscription status if available
                subscription = proxmox.nodes(node_name).subscription.get()
                status["subscription"] = subscription
            except:
                pass

            cluster_info["nodes"].append(status)

        except Exception as e:
            cluster_info["nodes"].append(
                {"host": node_info["name"], "error": str(e), "online": False}
            )

    # Sort nodes by name
    cluster_info["nodes"].sort(key=lambda x: x["host"])

    return render_template("cluster.html", cluster=cluster_info)


@app.route("/api/nodes")
def api_nodes():
    """API endpoint to get all cluster nodes"""
    nodes = []
    seen = set()
    for node_info in cluster_nodes:
        name = node_info["name"]
        # Guard against duplicate/blank node names reaching the UI: a duplicate
        # key crashes Alpine's keyed x-for and blanks the whole page.
        if not name or name in seen:
            continue
        seen.add(name)
        nodes.append({"name": name, "status": node_info.get("status", "unknown")})
    # Sort nodes alphabetically by name
    nodes.sort(key=lambda x: x["name"])
    return jsonify(nodes)


@app.route("/api/nodes/status")
def api_nodes_status():
    """API endpoint to get detailed status of all cluster nodes"""
    nodes_status = []

    for node_info in cluster_nodes:
        node_name = node_info["name"]
        if node_info.get("status") != "online":
            nodes_status.append(
                {"name": node_name, "online": False, "status": node_info.get("status")}
            )
            continue

        # Use the stored connection from cluster_nodes
        proxmox = node_info.get("connection")
        if not proxmox:
            # Fallback to looking up by node name
            proxmox = get_proxmox_connection(node_name, auto_renew=True)

        if not proxmox:
            nodes_status.append(
                {"name": node_name, "online": False, "error": "No connection"}
            )
            continue

        try:
            status = proxmox.nodes(node_name).status.get()
            # Use same format as cluster page
            cpu = float(status.get("cpu", 0))
            memory = status.get("memory", {})
            mem_used = memory.get("used", 0)
            mem_total = memory.get("total", 1)

            nodes_status.append(
                {
                    "name": node_name,
                    "online": True,
                    "cpu": cpu,
                    "mem_used": mem_used,
                    "mem_total": mem_total,
                    "uptime": status.get("uptime", 0),
                }
            )
        except Exception as e:
            nodes_status.append({"name": node_name, "online": False, "error": str(e)})

    # Sort nodes alphabetically by name
    nodes_status.sort(key=lambda x: x["name"])
    return jsonify(nodes_status)


@app.route("/api/resources")
def api_resources():
    """API endpoint to get all resources"""
    resources = get_all_vms_and_containers()
    return jsonify(resources)


@app.route("/api/node/<node>/status")
def api_node_status(node):
    """API endpoint to get specific node status"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        status = proxmox.nodes(node).status.get()
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/node/<node>/storages")
def api_node_storages(node):
    """API endpoint to get storage pools for a specific node"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    # Get vm_type from query parameter (qemu or lxc)
    vm_type = request.args.get("vm_type", "qemu")

    try:
        storages = proxmox.nodes(node).storage.get()
        # Filter storages based on VM type
        vm_storages = []
        for storage in storages:
            if storage.get("enabled", 0) == 1:
                content_types = storage.get("content", "").split(",")

                # Filter based on VM type
                is_suitable = False
                if vm_type == "qemu" and "images" in content_types:
                    is_suitable = True
                elif vm_type == "lxc" and "rootdir" in content_types:
                    is_suitable = True

                if is_suitable:
                    # Calculate available space
                    if storage.get("total") and storage.get("total") > 0:
                        storage["available_bytes"] = storage.get(
                            "total", 0
                        ) - storage.get("used", 0)
                        storage["available_gb"] = round(
                            storage["available_bytes"] / (1024**3), 2
                        )
                        storage["used_percent"] = (
                            storage.get("used", 0) / storage["total"]
                        ) * 100
                    else:
                        storage["available_bytes"] = 0
                        storage["available_gb"] = 0
                        storage["used_percent"] = 0

                    vm_storages.append(storage)

        # Sort by available space (descending - most space first)
        vm_storages.sort(key=lambda x: x["available_bytes"], reverse=True)
        return jsonify(vm_storages)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/node/<node>/templates")
def api_node_templates(node):
    """API endpoint to get VM templates and container templates for a specific node"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        result = {"qemu": [], "lxc": []}  # VM templates  # Container templates

        # Get VM templates
        try:
            vms = proxmox.nodes(node).qemu.get()
            templates = [vm for vm in vms if vm.get("template") == 1]
            result["qemu"] = templates
        except Exception as e:
            print(f"Error getting VM templates: {e}")

        # Get container templates
        try:
            storages = proxmox.nodes(node).storage.get()
            for storage in storages:
                if (
                    "vztmpl" in storage.get("content", "").split(",")
                    and storage.get("enabled", 0) == 1
                ):
                    # Get templates in this storage
                    try:
                        templates = (
                            proxmox.nodes(node)
                            .storage(storage["storage"])
                            .content.get(content="vztmpl")
                        )
                        result["lxc"].extend(templates)
                    except Exception as e:
                        print(
                            f"Error getting templates from storage {storage.get('storage')}: {e}"
                        )
        except Exception as e:
            print(f"Error getting LXC templates: {e}")

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/node/<node>/iso-images")
def api_node_iso_images(node):
    """API endpoint to get ISO images for a specific node"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        iso_images = []

        # Get all storages and look for ISO content
        storages = proxmox.nodes(node).storage.get()
        for storage in storages:
            if storage.get("enabled", 0) == 1 and "iso" in storage.get(
                "content", ""
            ).split(","):
                # Get ISO files in this storage
                try:
                    isos = (
                        proxmox.nodes(node)
                        .storage(storage["storage"])
                        .content.get(content="iso")
                    )
                    iso_images.extend(isos)
                except Exception as e:
                    print(
                        f"Error getting ISOs from storage {storage.get('storage')}: {e}"
                    )

        # Sort by filename
        iso_images.sort(key=lambda x: x.get("volid", "").split("/")[-1].lower())

        return jsonify(iso_images)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/node/<node>/iso-storages")
def api_node_iso_storages(node):
    """API endpoint to get storage pools that support ISO content for a specific node"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        storages = proxmox.nodes(node).storage.get()
        iso_storages = []

        for storage in storages:
            if storage.get("enabled", 0) == 1 and "iso" in storage.get(
                "content", ""
            ).split(","):
                # Calculate available space
                if storage.get("total") and storage.get("total") > 0:
                    storage["available_bytes"] = storage.get("total", 0) - storage.get(
                        "used", 0
                    )
                    storage["available_gb"] = round(
                        storage["available_bytes"] / (1024**3), 2
                    )
                    storage["used_percent"] = (
                        storage.get("used", 0) / storage["total"]
                    ) * 100
                else:
                    storage["available_bytes"] = 0
                    storage["available_gb"] = 0
                    storage["used_percent"] = 0

                iso_storages.append(storage)

        # Sort by available space (descending - most space first)
        iso_storages.sort(key=lambda x: x["available_bytes"], reverse=True)
        return jsonify(iso_storages)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/node/<node>/download-iso", methods=["POST"])
def api_node_download_iso(node):
    """API endpoint to download ISO from URL to a specific node storage"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        data = request.get_json()
        if not data or "url" not in data or "storage" not in data:
            return jsonify({"error": "Missing URL or storage parameter"}), 400

        url = data["url"].strip() if isinstance(data["url"], str) else data["url"]
        storage = data["storage"]
        filename = data.get("filename", "")

        if not is_allowed_download_url(url):
            return (
                jsonify(
                    {
                        "error": "Only http:// and https:// URLs without embedded credentials are supported"
                    }
                ),
                400,
            )

        # If no filename provided, extract from URL
        if not filename:
            filename = filename_from_url(url)

        # Validate filename
        if not is_safe_download_filename(filename, (".iso",)):
            return (
                jsonify(
                    {
                        "error": "Filename must be a simple .iso filename without path separators"
                    }
                ),
                400,
            )

        # Check Proxmox version and try appropriate download method
        try:
            # Get Proxmox version to determine API capabilities
            version_info = proxmox.version.get()

            # Try the newer download-url endpoint (PVE 7.0+)
            try:
                result = (
                    proxmox.nodes(node)
                    .storage(storage)("download-url")
                    .post(content="iso", filename=filename, url=url)
                )
            except Exception as download_error:
                # If download-url doesn't work, provide fallback message
                if "not implemented" in str(download_error).lower() or "501" in str(
                    download_error
                ):
                    return (
                        jsonify(
                            {
                                "error": "Direct URL download is not supported on this Proxmox version. Please download the ISO manually and upload it to the storage.",
                            }
                        ),
                        501,
                    )
                else:
                    # Other error, re-raise
                    raise download_error

        except Exception as e:
            # Handle any other errors
            return jsonify({"error": f"Failed to start download: {str(e)}"}), 500

        return jsonify(
            {
                "success": True,
                "message": f"Download started for {filename}",
                "task": result,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/node/<node>/iso/<path:volid>", methods=["DELETE"])
def api_delete_iso(node, volid):
    """API endpoint to delete an ISO image"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        # volid format: storage/filename.iso
        parts = volid.split("/")
        if len(parts) != 2:
            return jsonify({"error": "Invalid volume ID format"}), 400

        storage = parts[0]
        proxmox.nodes(node).storage(storage).content(volid).delete()

        return jsonify(
            {"success": True, "message": f"ISO {volid} deleted successfully"}
        )

    except Exception as e:
        return jsonify({"error": f"Failed to delete ISO: {str(e)}"}), 500


@app.route("/api/node/<node>/template/<vmid>", methods=["DELETE"])
def api_delete_vm_template(node, vmid):
    """API endpoint to delete a VM template"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        # Verify it's a template
        config = proxmox.nodes(node).qemu(vmid).config.get()
        if not config.get("template"):
            return jsonify({"error": "VM is not a template"}), 400

        # Delete the template (purge=1 removes disks too)
        proxmox.nodes(node).qemu(vmid).delete(purge=1)

        return jsonify(
            {"success": True, "message": f"Template {vmid} deleted successfully"}
        )

    except Exception as e:
        error_msg = str(e)
        if "does not exist" in error_msg.lower():
            return jsonify({"error": f"Template {vmid} does not exist"}), 404
        return jsonify({"error": f"Failed to delete template: {error_msg}"}), 500


@app.route("/api/node/<node>/lxc-template/<path:volid>", methods=["DELETE"])
def api_delete_lxc_template(node, volid):
    """API endpoint to delete an LXC container template"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        # volid format: storage/template-name.tar.xz
        parts = volid.split("/")
        if len(parts) != 2:
            return jsonify({"error": "Invalid volume ID format"}), 400

        storage = parts[0]
        proxmox.nodes(node).storage(storage).content(volid).delete()

        return jsonify(
            {"success": True, "message": "Container template deleted successfully"}
        )

    except Exception as e:
        return jsonify({"error": f"Failed to delete template: {str(e)}"}), 500


@app.route("/api/node/<node>/available-lxc-templates")
def api_available_lxc_templates(node):
    """API endpoint to get available LXC appliance templates from Proxmox repo"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        # Get available appliance templates from Proxmox repository
        templates = proxmox.nodes(node).aplinfo.get()

        # Group templates by section/category
        grouped = {}
        for template in templates:
            section = template.get("section", "other")
            if section not in grouped:
                grouped[section] = []

            grouped[section].append(
                {
                    "template": template.get("template", ""),
                    "headline": template.get("headline", ""),
                    "description": template.get("description", ""),
                    "os": template.get("os", ""),
                    "version": template.get("version", ""),
                    "package": template.get("package", ""),
                    "source": template.get("source", ""),
                    "sha512sum": template.get("sha512sum", ""),
                    "infopage": template.get("infopage", ""),
                }
            )

        # Sort templates within each section by template name
        for section in grouped:
            grouped[section].sort(key=lambda x: x["template"])

        return jsonify({"templates": grouped, "total": len(templates)})

    except Exception as e:
        return jsonify({"error": f"Failed to get available templates: {str(e)}"}), 500


def run_lxc_template_download_job(job_id, node, params):
    """Background job to download LXC container template and track progress"""
    job_queue.set_running(job_id)

    try:
        proxmox = get_proxmox_connection(node, auto_renew=True)
        if not proxmox:
            job_queue.set_failed(job_id, "Failed to connect to node")
            return

        source_type = params.get("source_type", "proxmox")
        storage = params.get("storage")
        template_name = params.get("template_name", "template")

        job_queue.add_step(job_id, f"Starting {source_type} download...")
        job_queue.update_job(job_id, progress=5)

        task_upid = None

        if source_type == "proxmox":
            template = params.get("template")
            job_queue.add_step(job_id, f"Downloading template: {template}")
            job_queue.update_job(job_id, progress=10)

            # Download from Proxmox appliance repository
            task_upid = proxmox.nodes(node).aplinfo.post(
                storage=storage, template=template
            )
            job_queue.add_step(job_id, f"Download task started: {task_upid}")

        elif source_type == "url":
            url = params.get("url")
            filename = params.get("filename")
            job_queue.add_step(job_id, f"Downloading from URL: {url}")
            job_queue.update_job(job_id, progress=10)

            # Download from URL
            task_upid = (
                proxmox.nodes(node)
                .storage(storage)("download-url")
                .post(url=url, filename=filename, content="vztmpl")
            )
            job_queue.add_step(job_id, f"Download task started: {task_upid}")

        elif source_type == "oci":
            image = params.get("image")
            filename = params.get("filename")
            # Remove docker:// prefix for the API call
            image_ref = (
                image.replace("docker://", "")
                if image.startswith("docker://")
                else image
            )
            job_queue.add_step(job_id, f"Node: {node}, Storage: {storage}")
            job_queue.add_step(job_id, f"Downloading OCI image: {image_ref}")
            job_queue.add_step(job_id, f"Output filename: {filename}")
            job_queue.update_job(job_id, progress=10)

            # OCI images are downloaded via oci-registry-pull endpoint (Proxmox 8+)
            # Use direct path construction for proxmoxer compatibility
            # API requires 'reference' parameter (not 'image')
            try:
                storage_api = proxmox.nodes(node).storage(storage)
                task_upid = storage_api.post(
                    "oci-registry-pull", reference=image_ref, filename=filename
                )
                job_queue.add_step(job_id, f"Download task started: {task_upid}")
            except Exception as oci_error:
                job_queue.set_failed(
                    job_id,
                    f"OCI download failed: {str(oci_error)}",
                )
                return

        if not task_upid:
            job_queue.set_failed(job_id, "Failed to start download task")
            return

        # Wait for the download task to complete
        job_queue.add_step(job_id, "Waiting for download to complete...")
        job_queue.update_job(job_id, progress=20)

        result = wait_for_task(proxmox, node, task_upid, job_id, timeout=1800)

        if not result["success"]:
            job_queue.set_failed(
                job_id, f"Download failed: {result.get('error', 'Unknown error')}"
            )
            return

        job_queue.add_step(job_id, "Download completed successfully!")
        job_queue.update_job(job_id, progress=100)

        job_queue.set_completed(
            job_id,
            {
                "template": template_name,
                "storage": storage,
                "source_type": source_type,
            },
        )

    except Exception as e:
        job_queue.set_failed(job_id, str(e))


@app.route("/api/node/<node>/download-lxc-template", methods=["POST"])
def api_download_lxc_template(node):
    """API endpoint to download LXC container template from various sources"""
    if DEMO_MODE:
        return (
            jsonify({"error": "Template download is disabled in demo mode"}),
            403,
        )

    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    storage = data.get("storage")
    if not storage:
        return jsonify({"error": "Storage is required"}), 400

    source_type = data.get("source_type", "proxmox")  # proxmox, url, or oci

    # Validate inputs based on source type
    template_name = ""
    params = {"storage": storage, "source_type": source_type}

    try:
        if source_type == "proxmox":
            template = data.get("template")
            if not template:
                return jsonify({"error": "Template name is required"}), 400
            params["template"] = template
            template_name = template

        elif source_type == "url":
            url = data.get("url")
            if not url:
                return jsonify({"error": "URL is required"}), 400
            url = url.strip() if isinstance(url, str) else url

            if not is_allowed_download_url(url):
                return (
                    jsonify(
                        {
                            "error": "Only http:// and https:// URLs without embedded credentials are supported"
                        }
                    ),
                    400,
                )

            filename = data.get("filename")
            if not filename:
                filename = filename_from_url(url)

            if not is_safe_download_filename(
                filename, (".tar.xz", ".tar.gz", ".tar.zst")
            ):
                return (
                    jsonify(
                        {
                            "error": "Filename must be a simple .tar.xz, .tar.gz, or .tar.zst filename without path separators"
                        }
                    ),
                    400,
                )

            params["url"] = url
            params["filename"] = filename
            template_name = filename

        elif source_type == "oci":
            image = data.get("image")
            if not image:
                return jsonify({"error": "OCI image reference is required"}), 400

            # Normalize the image reference
            if not image.startswith("docker://"):
                image = f"docker://{image}"

            # Generate filename from image reference
            # e.g., docker://docker.io/library/alpine:3.14 -> alpine-3.14.tar
            # OCI images use .tar extension (Proxmox adds compression internally)
            image_ref = image.replace("docker://", "")
            image_name = image_ref.split("/")[-1]  # alpine:3.14
            if ":" in image_name:
                name, tag = image_name.split(":", 1)
                filename = f"{name}-{tag}.tar"
            else:
                filename = f"{image_name}-latest.tar"

            # Sanitize filename (replace invalid chars)
            filename = filename.replace("/", "-").replace(":", "-")

            params["image"] = image
            params["filename"] = filename
            template_name = filename

        else:
            return jsonify({"error": f"Unknown source type: {source_type}"}), 400

        params["template_name"] = template_name
        params["node"] = node

        # Create background job
        job_id = job_queue.create_job(
            job_type="lxc_template_download",
            description=f"Download LXC template: {template_name}",
            params=params,
        )

        # Start background thread
        thread = threading.Thread(
            target=run_lxc_template_download_job,
            args=(job_id, node, params),
            daemon=True,
        )
        thread.start()

        return jsonify(
            {
                "success": True,
                "message": f"Download started for {template_name}. Track progress in the Jobs dropdown.",
                "job_id": job_id,
            }
        )

    except Exception as e:
        error_msg = str(e)
        return jsonify({"error": f"Failed to start download: {error_msg}"}), 500


@app.route("/api/node/<node>/lxc-storages")
def api_lxc_storages(node):
    """API endpoint to get storages that support container templates (vztmpl)"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        storages = proxmox.nodes(node).storage.get()

        # Filter for storages that can hold container templates
        lxc_storages = []
        for storage in storages:
            # Skip disabled storages
            if storage.get("enabled") == 0 or storage.get("active") == 0:
                continue

            content_types = storage.get("content", "").split(",")
            # Check if storage supports vztmpl (container templates)
            if "vztmpl" in content_types:
                available_bytes = storage.get("avail", 0)
                total_bytes = storage.get("total", 0)
                lxc_storages.append(
                    {
                        "storage": storage.get("storage"),
                        "type": storage.get("type"),
                        "content": storage.get("content"),
                        "available": available_bytes,
                        "available_gb": (
                            round(available_bytes / (1024**3), 2)
                            if available_bytes
                            else 0
                        ),
                        "total": total_bytes,
                        "total_gb": (
                            round(total_bytes / (1024**3), 2) if total_bytes else 0
                        ),
                    }
                )

        return jsonify(lxc_storages)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/node/<node>/lxc", methods=["POST"])
def api_create_lxc(node):
    """API endpoint to create a new LXC container from a template"""
    if DEMO_MODE:
        return (
            jsonify({"error": "Container creation is disabled in demo mode"}),
            403,
        )

    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    # Required parameters
    ostemplate = data.get("ostemplate")
    hostname = data.get("hostname")
    storage = data.get("storage")

    if not ostemplate:
        return jsonify({"error": "Template (ostemplate) is required"}), 400
    if not hostname:
        return jsonify({"error": "Hostname is required"}), 400
    if not storage:
        return jsonify({"error": "Storage is required"}), 400

    try:
        # Get next available VMID if not provided
        vmid = data.get("vmid")
        if not vmid:
            vmid = proxmox.cluster.nextid.get()

        # Build container creation parameters
        params = {
            "vmid": int(vmid),
            "ostemplate": ostemplate,
            "hostname": hostname,
            "storage": storage,
            "rootfs": f"{storage}:{data.get('rootfs_size', 8)}",
            "cores": int(data.get("cores", 1)),
            "memory": int(data.get("memory", 512)),
            "swap": int(data.get("swap", 512)),
            "unprivileged": 1 if data.get("unprivileged", True) else 0,
            "start": 1 if data.get("start", False) else 0,
        }

        # Network configuration
        if data.get("net0"):
            params["net0"] = data["net0"]

        # Password (optional)
        if data.get("password"):
            params["password"] = data["password"]

        # SSH public keys (optional)
        if data.get("ssh_public_keys"):
            params["ssh-public-keys"] = data["ssh_public_keys"]

        # Create the container
        task = proxmox.nodes(node).lxc.post(**params)

        # Update next-id if incremental VMID is enabled
        update_cluster_next_id(proxmox, vmid)

        return jsonify(
            {
                "success": True,
                "message": f"Container {vmid} creation started",
                "vmid": vmid,
                "upid": task,
            }
        )

    except Exception as e:
        error_msg = str(e)
        return jsonify({"error": f"Failed to create container: {error_msg}"}), 500


@app.route("/api/node/<node>/networks")
def api_node_networks(node):
    """API endpoint to get network interfaces for a specific node"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        networks = proxmox.nodes(node).network.get()
        # Filter for bridge interfaces
        bridges = [net for net in networks if net.get("type") == "bridge"]
        return jsonify(bridges)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cluster/nextid")
def api_next_vmid():
    """API endpoint to get the next available VMID"""
    if not proxmox_nodes:
        return jsonify({"error": "No Proxmox connections available"}), 500

    try:
        # Get any working connection with auto-renewal
        node_name = next(iter(proxmox_nodes.keys()))
        proxmox = get_proxmox_connection(node_name, auto_renew=True)

        if not proxmox:
            return (
                jsonify(
                    {"error": "No valid Proxmox connection available", "vmid": 100}
                ),
                500,
            )

        # Get next available VMID
        next_id = proxmox.cluster.nextid.get()
        return jsonify({"vmid": next_id})
    except Exception as e:
        return jsonify({"error": str(e), "vmid": 100}), 500


@app.route("/api/cluster/vmid/<vmid>/check")
def api_check_vmid(vmid):
    """API endpoint to check if a VMID is available (not in use)"""
    if not proxmox_nodes:
        return jsonify({"error": "No Proxmox connections available"}), 500

    try:
        # Validate VMID is a number
        vmid_int = int(vmid)
        if vmid_int < 100:
            return jsonify({"available": False, "reason": "VMID must be >= 100"})

        # Get any working connection with auto-renewal
        node_name = next(iter(proxmox_nodes.keys()))
        proxmox = get_proxmox_connection(node_name, auto_renew=True)

        if not proxmox:
            return jsonify({"error": "No valid Proxmox connection available"}), 500

        # Get all VMs and containers from cluster resources
        resources = proxmox.cluster.resources.get(type="vm")

        # Check if VMID is in use
        for resource in resources:
            if resource.get("vmid") == vmid_int:
                return jsonify(
                    {
                        "available": False,
                        "reason": f"VMID {vmid} is already in use",
                        "name": resource.get("name", ""),
                        "node": resource.get("node", ""),
                        "type": resource.get("type", ""),
                    }
                )

        return jsonify({"available": True})

    except ValueError:
        return jsonify({"available": False, "reason": "Invalid VMID format"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<node>/<vmid>/tasks")
def api_vm_tasks(node, vmid):
    """API endpoint to get recent tasks for a specific VM/container"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        # Get all tasks from the node
        all_tasks = proxmox.nodes(node).tasks.get()

        # Filter tasks related to this specific VM/container
        vm_tasks = []
        for task in all_tasks:
            # Check if task is related to this VMID
            task_description = task.get("type", "").lower()
            task_id = task.get("id", "")

            # Match tasks that contain the VMID or are VM-related operations
            if (
                vmid in task_id
                or f":{vmid}:" in task_id
                or task_id.endswith(f":{vmid}")
                or task_id.startswith(f"{vmid}:")
                or f"VMID {vmid}" in task.get("status", "")
                or (
                    task.get("type")
                    in [
                        "qmstart",
                        "qmstop",
                        "qmshutdown",
                        "qmreset",
                        "qmigrate",
                        "qmclone",
                        "qmcreate",
                        "qmdestroy",
                        "vzstart",
                        "vzstop",
                        "vzshutdown",
                        "vzmigrate",
                        "vzclone",
                        "vzcreate",
                        "vzdestroy",
                    ]
                    and vmid in str(task.get("id", ""))
                )
            ):

                # Add human-readable timestamps
                if task.get("starttime"):
                    task["start_time_formatted"] = datetime.fromtimestamp(
                        task["starttime"]
                    ).strftime("%Y-%m-%d %H:%M:%S")
                if task.get("endtime"):
                    task["end_time_formatted"] = datetime.fromtimestamp(
                        task["endtime"]
                    ).strftime("%Y-%m-%d %H:%M:%S")

                # Add status badge class for UI
                status = task.get("status", "").lower()
                if "ok" in status or status == "stopped":
                    task["status_class"] = "success"
                elif "error" in status or "failed" in status:
                    task["status_class"] = "danger"
                elif "running" in status:
                    task["status_class"] = "primary"
                else:
                    task["status_class"] = "secondary"

                vm_tasks.append(task)

        # Sort by start time (most recent first)
        vm_tasks.sort(key=lambda x: x.get("starttime", 0), reverse=True)

        # Limit to last 10 tasks
        vm_tasks = vm_tasks[:10]

        return jsonify(vm_tasks)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tasks")
def api_cluster_tasks():
    """API endpoint to get all recent tasks from the cluster using /cluster/tasks endpoint"""
    if not proxmox_nodes:
        return jsonify({"error": "No Proxmox connections available"}), 500

    try:
        # Get any working connection to access cluster endpoint with auto-renewal
        node_name = next(iter(proxmox_nodes.keys()))
        proxmox = get_proxmox_connection(node_name, auto_renew=True)

        if not proxmox:
            return jsonify({"error": "No valid Proxmox connection available"}), 500

        # Get limit from query parameter, default to 50
        limit = request.args.get("limit", 50, type=int)

        # Use Proxmox cluster tasks endpoint which includes all tasks (including running ones)
        cluster_tasks = proxmox.cluster.tasks.get()

        processed_tasks = []
        for task in cluster_tasks[:limit]:
            # Add human-readable timestamps
            if task.get("starttime"):
                task["start_time_formatted"] = datetime.fromtimestamp(
                    task["starttime"]
                ).strftime("%Y-%m-%d %H:%M:%S")
            if task.get("endtime"):
                task["end_time_formatted"] = datetime.fromtimestamp(
                    task["endtime"]
                ).strftime("%Y-%m-%d %H:%M:%S")

            # Add status badge class for UI
            status = task.get("status", "running").lower()
            has_endtime = task.get("endtime") is not None

            if "ok" in status or status == "stopped":
                task["status_class"] = "success"
            elif "error" in status or "failed" in status:
                task["status_class"] = "danger"
            elif "running" in status or not has_endtime:
                # Task is explicitly running or has no end time (still in progress)
                task["status_class"] = "primary"
            else:
                task["status_class"] = "secondary"

            # Extract VMID from task UPID if possible
            upid = task.get("upid", "")
            task_type = task.get("type", "")

            # Parse UPID format: UPID:node:pid:starttime:type:id:user@realm:status
            if upid and task_type in [
                "qmstart",
                "qmstop",
                "qmshutdown",
                "qmreset",
                "qmreboot",
                "qmigrate",
                "qmclone",
                "qmcreate",
                "qmdestroy",
                "qmtemplate",
                "qmdisk",
                "vzstart",
                "vzstop",
                "vzshutdown",
                "vzmigrate",
                "vzclone",
                "vzcreate",
                "vzdestroy",
                "vzdump",
            ]:
                try:
                    upid_parts = upid.split(":")
                    if len(upid_parts) >= 6:
                        # The 6th part (index 5) should be the VMID for VM/CT operations
                        potential_vmid = upid_parts[5]
                        if potential_vmid.isdigit():
                            task["vmid"] = potential_vmid
                except:
                    pass  # Ignore parsing errors

            processed_tasks.append(task)

        # Sort by start time (most recent first)
        processed_tasks.sort(key=lambda x: x.get("starttime", 0), reverse=True)

        return jsonify(processed_tasks)

    except Exception as e:
        print(f"Error getting cluster tasks: {e}")
        return jsonify({"error": f"Failed to retrieve tasks: {str(e)}"}), 500


@app.route("/api/vm/<node>/<vmid>/metrics")
def api_vm_metrics(node, vmid):
    """API endpoint to get historical metrics for a specific VM/container"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    # Get timeframe from query parameter, default to 'day'
    timeframe = request.args.get("timeframe", "day")

    # Map timeframes to Proxmox RRD timeframes
    timeframe_mapping = {
        "hour": "hour",
        "day": "day",
        "week": "week",
        "month": "month",
        "year": "year",
    }

    proxmox_timeframe = timeframe_mapping.get(timeframe, "day")

    try:
        # Determine VM type
        vm_type = "qemu"
        try:
            proxmox.nodes(node).qemu(vmid).config.get()
        except:
            vm_type = "lxc"

        # Get current status for current values
        if vm_type == "qemu":
            status = proxmox.nodes(node).qemu(vmid).status.current.get()
            # Get historical RRD data
            try:
                rrd_data = (
                    proxmox.nodes(node)
                    .qemu(vmid)
                    .rrddata.get(timeframe=proxmox_timeframe)
                )
            except:
                rrd_data = []
        else:
            status = proxmox.nodes(node).lxc(vmid).status.current.get()
            # Get historical RRD data for containers
            try:
                rrd_data = (
                    proxmox.nodes(node)
                    .lxc(vmid)
                    .rrddata.get(timeframe=proxmox_timeframe)
                )
            except:
                rrd_data = []

        # Process historical data for charts
        chart_data = {
            "labels": [],
            "cpu": [],
            "memory": [],
            "network_in": [],
            "network_out": [],
            "disk_read": [],
            "disk_write": [],
        }

        max_memory = status.get("maxmem", 1)

        for entry in rrd_data:
            timestamp = entry.get("time", 0)
            if timestamp:
                # Format timestamp based on timeframe
                dt = datetime.fromtimestamp(timestamp)
                if timeframe == "hour":
                    label = dt.strftime("%H:%M")
                elif timeframe == "day":
                    label = dt.strftime("%H:%M")
                elif timeframe == "week":
                    label = dt.strftime("%m/%d %H:%M")
                elif timeframe == "month":
                    label = dt.strftime("%m/%d")
                else:  # year
                    label = dt.strftime("%Y-%m")

                chart_data["labels"].append(label)
                chart_data["cpu"].append(
                    (entry.get("cpu", 0) * 100) if entry.get("cpu") is not None else 0
                )
                chart_data["memory"].append(
                    (entry.get("mem", 0) / max_memory * 100)
                    if entry.get("mem") and max_memory
                    else 0
                )
                chart_data["network_in"].append(
                    entry.get("netin", 0) / (1024 * 1024) if entry.get("netin") else 0
                )  # Convert to MB
                chart_data["network_out"].append(
                    entry.get("netout", 0) / (1024 * 1024) if entry.get("netout") else 0
                )  # Convert to MB
                chart_data["disk_read"].append(
                    entry.get("diskread", 0) / (1024 * 1024)
                    if entry.get("diskread")
                    else 0
                )  # Convert to MB
                chart_data["disk_write"].append(
                    entry.get("diskwrite", 0) / (1024 * 1024)
                    if entry.get("diskwrite")
                    else 0
                )  # Convert to MB

        # Current metrics for display
        current_metrics = {
            "vm_type": vm_type,
            "status": status.get("status", "unknown"),
            "uptime": status.get("uptime", 0),
            "cpu": {
                "current": (
                    status.get("cpu", 0) * 100 if status.get("cpu") is not None else 0
                ),
                "cores": status.get("cpus", status.get("maxcpu", 1)),
            },
            "memory": {
                "used": status.get("mem", 0),
                "max": status.get("maxmem", 0),
                "usage_percent": (
                    (status.get("mem", 0) / status.get("maxmem", 1)) * 100
                    if status.get("maxmem")
                    else 0
                ),
                "used_gb": status.get("mem", 0) / (1024**3) if status.get("mem") else 0,
                "max_gb": (
                    status.get("maxmem", 0) / (1024**3) if status.get("maxmem") else 0
                ),
            },
            "disk": {
                "used": status.get("disk", 0),
                "max": status.get("maxdisk", 0),
                "usage_percent": (
                    (status.get("disk", 0) / status.get("maxdisk", 1)) * 100
                    if status.get("maxdisk")
                    else 0
                ),
                "used_gb": (
                    status.get("disk", 0) / (1024**3) if status.get("disk") else 0
                ),
                "max_gb": (
                    status.get("maxdisk", 0) / (1024**3) if status.get("maxdisk") else 0
                ),
            },
        }

        response = {
            "current": current_metrics,
            "historical": chart_data,
            "timeframe": timeframe,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        return jsonify(response)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<node>/<vmid>/status")
def api_vm_status(node, vmid):
    """Lightweight current-status endpoint for live polling on the detail page."""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        vm_type = "qemu"
        try:
            proxmox.nodes(node).qemu(vmid).config.get()
            status = proxmox.nodes(node).qemu(vmid).status.current.get()
        except Exception:
            vm_type = "lxc"
            status = proxmox.nodes(node).lxc(vmid).status.current.get()

        return jsonify(
            {
                "status": status.get("status", "unknown"),
                "uptime": status.get("uptime", 0),
                "cpu": status.get("cpu", 0),
                "mem": status.get("mem", 0),
                "maxmem": status.get("maxmem", 0),
                # 'lock' is set on the VM being cloned/migrated/backed up/etc.
                # (Proxmox reports it on the affected VM itself, source or target.)
                "lock": status.get("lock", ""),
                "vm_type": vm_type,
            }
        )
    except Exception as e:
        return _proxmox_error_response(e)


# VM Configuration API
@app.route("/api/vm/<node>/<vmid>/config", methods=["GET", "PUT"])
def api_vm_config(node, vmid):
    """Get or update VM/Container configuration"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    # Determine if this is a QEMU VM or LXC container
    vm_type = "qemu"
    try:
        proxmox.nodes(node).qemu(vmid).status.current.get()
    except:
        vm_type = "lxc"

    try:
        if request.method == "GET":
            # Get VM/Container configuration
            if vm_type == "qemu":
                config = proxmox.nodes(node).qemu(vmid).config.get()
            else:
                config = proxmox.nodes(node).lxc(vmid).config.get()
            config["_vm_type"] = vm_type  # Include type in response
            return jsonify(config)

        elif request.method == "PUT":
            # Update VM configuration
            data = request.get_json()
            if not data:
                return jsonify({"error": "No configuration data provided"}), 400

            # Build update parameters
            params = {}

            # CPU configuration
            if "cpu" in data:
                params["cpu"] = data["cpu"]
            if "sockets" in data:
                params["sockets"] = int(data["sockets"])
            if "cores" in data:
                params["cores"] = int(data["cores"])

            # Memory configuration
            if "memory" in data:
                params["memory"] = int(data["memory"])

            # LXC-specific: Swap configuration
            if "swap" in data and vm_type == "lxc":
                params["swap"] = int(data["swap"])

            # LXC-specific: Hostname
            if "hostname" in data and vm_type == "lxc":
                params["hostname"] = data["hostname"]

            # Boot configuration
            if "onboot" in data:
                params["onboot"] = int(data["onboot"])

            # Display configuration (QEMU only)
            if "vga" in data and vm_type == "qemu":
                params["vga"] = data["vga"]

            # Network interfaces (net0, net1, etc.)
            import re

            delete_params = []
            for key, value in data.items():
                if key.startswith("net") and re.match(r"^net\d+$", key):
                    if value == "" or value is None:
                        # Mark for deletion
                        delete_params.append(key)
                    else:
                        params[key] = value

            # Disk interfaces (scsi0, virtio0, ide0, sata0, etc.)
            for key, value in data.items():
                if re.match(r"^(scsi|virtio|ide|sata)\d+$", key):
                    params[key] = value

            # Add delete parameter for removing interfaces
            if delete_params:
                params["delete"] = ",".join(delete_params)

            # Update VM/Container configuration
            if vm_type == "qemu":
                proxmox.nodes(node).qemu(vmid).config.put(**params)
            else:
                proxmox.nodes(node).lxc(vmid).config.put(**params)

            return jsonify(
                {"success": True, "message": "Configuration updated successfully"}
            )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<node>/<vmid>/resize-disk", methods=["PUT"])
def api_vm_resize_disk(node, vmid):
    """Resize VM/Container disk"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    # Determine if this is a QEMU VM or LXC container
    vm_type = "qemu"
    try:
        proxmox.nodes(node).qemu(vmid).status.current.get()
    except:
        vm_type = "lxc"

    try:
        data = request.get_json()
        if not data or "disk" not in data or "size" not in data:
            return jsonify({"error": "Missing disk or size parameter"}), 400

        disk = data["disk"]
        size = data["size"]

        # Validate size format (should be like "32G")
        if isinstance(size, int):
            size = f"{size}G"
        elif isinstance(size, str) and not size.endswith("G"):
            size = f"{size}G"

        # Use Proxmox API to resize disk
        if vm_type == "qemu":
            result = proxmox.nodes(node).qemu(vmid).resize.put(disk=disk, size=size)
        else:
            result = proxmox.nodes(node).lxc(vmid).resize.put(disk=disk, size=size)

        return jsonify(
            {
                "success": True,
                "message": f"Disk {disk} resized to {size}",
                "result": result,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<node>/<vmid>/iso/attach", methods=["POST"])
def api_vm_iso_attach(node, vmid):
    """Attach ISO image to VM/Container"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        data = request.get_json()
        if not data or "iso_image" not in data:
            return jsonify({"error": "Missing iso_image parameter"}), 400

        iso_image = data["iso_image"]
        interface = data.get("interface", "ide2")  # Default to ide2 for CD-ROM

        # Determine VM type and get configuration
        vm_type = "qemu"
        try:
            config = proxmox.nodes(node).qemu(vmid).config.get()
            vm_obj = proxmox.nodes(node).qemu(vmid)
        except:
            vm_type = "lxc"
            config = proxmox.nodes(node).lxc(vmid).config.get()
            vm_obj = proxmox.nodes(node).lxc(vmid)

        # LXC containers don't support ISO attachment
        if vm_type == "lxc":
            return (
                jsonify({"error": "LXC containers do not support ISO attachment"}),
                400,
            )

        if interface in config:
            return jsonify({"error": f"Interface {interface} is already in use"}), 400

        # Attach ISO to specified interface
        update_params = {interface: f"{iso_image},media=cdrom"}

        vm_obj.config.put(**update_params)

        return jsonify(
            {
                "success": True,
                "message": f"ISO {iso_image} attached to {interface}",
                "interface": interface,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<node>/<vmid>/iso/detach", methods=["POST"])
def api_vm_iso_detach(node, vmid):
    """Detach ISO image from VM/Container"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        data = request.get_json()
        if not data or "interface" not in data:
            return jsonify({"error": "Missing interface parameter"}), 400

        interface = data["interface"]

        # Determine VM type and get configuration
        vm_type = "qemu"
        try:
            config = proxmox.nodes(node).qemu(vmid).config.get()
            vm_obj = proxmox.nodes(node).qemu(vmid)
        except:
            vm_type = "lxc"
            config = proxmox.nodes(node).lxc(vmid).config.get()
            vm_obj = proxmox.nodes(node).lxc(vmid)

        # LXC containers don't support ISO attachment/detachment
        if vm_type == "lxc":
            return (
                jsonify({"error": "LXC containers do not support ISO attachment"}),
                400,
            )

        if interface not in config:
            return jsonify({"error": f"Interface {interface} not found"}), 404

        # Remove ISO by deleting the interface
        update_params = {"delete": interface}

        vm_obj.config.put(**update_params)

        return jsonify(
            {
                "success": True,
                "message": f"ISO detached from {interface}",
                "interface": interface,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<node>/<vmid>/boot-order", methods=["PUT"])
def api_vm_boot_order(node, vmid):
    """Update VM boot order"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        data = request.get_json()
        if not data or "boot_devices" not in data:
            return jsonify({"error": "Missing boot_devices parameter"}), 400

        boot_devices = data["boot_devices"]
        if not isinstance(boot_devices, list):
            return jsonify({"error": "boot_devices must be an array"}), 400

        # Determine VM type and get configuration
        vm_type = "qemu"
        try:
            config = proxmox.nodes(node).qemu(vmid).config.get()
            vm_obj = proxmox.nodes(node).qemu(vmid)
        except:
            vm_type = "lxc"
            return (
                jsonify(
                    {"error": "LXC containers do not support boot order configuration"}
                ),
                400,
            )

        # Build boot order string
        if len(boot_devices) == 0:
            # No boot devices specified, use default
            boot_order = "c"
        else:
            # Use the new "order=" format for specific devices
            boot_order = "order=" + ";".join(boot_devices)

        # Update boot configuration
        update_params = {"boot": boot_order}

        vm_obj.config.put(**update_params)

        return jsonify(
            {
                "success": True,
                "message": "Boot order updated successfully",
                "boot_order": boot_order,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<node>/<vmid>/clone", methods=["POST"])
def api_vm_clone(node, vmid):
    """Clone a VM"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        data = request.get_json()
        if not data or "name" not in data or "vmid" not in data:
            return jsonify({"error": "Missing name or vmid parameter"}), 400

        clone_name = data["name"]
        clone_vmid = data["vmid"]
        target_node = data.get("target_node", node)
        target_storage = data.get("target_storage", "")
        clone_type = data.get("clone_type", "full")
        description = data.get("description", "")
        agent_enabled = bool(data.get("agent", False))

        # Prepare clone parameters
        clone_params = {
            "newid": int(clone_vmid),
            "name": clone_name,
            "full": 1 if clone_type == "full" else 0,
        }

        # Add target node if different from source
        if target_node != node:
            clone_params["target"] = target_node

        # Add target storage if specified
        if target_storage:
            clone_params["storage"] = target_storage

        # Add description if provided
        if description:
            clone_params["description"] = description

        # Execute clone operation (returns the task UPID, filed on the source node)
        result = proxmox.nodes(node).qemu(vmid).clone.post(**clone_params)

        # Update next-id if incremental VMID is enabled
        update_cluster_next_id(proxmox, clone_vmid)

        # Track the clone (disk copy → configuring → done) as a job so it shows
        # in the Jobs panel; the job also applies the guest-agent toggle once the
        # clone's lock releases.
        job_id = job_queue.create_job(
            job_type="clone",
            description=f"Clone VM {vmid} → {clone_vmid} ({clone_name})",
            params={
                "source_node": node,
                "target_node": target_node,
                "target_vmid": clone_vmid,
                "agent": agent_enabled,
            },
        )
        threading.Thread(
            target=run_clone_job,
            args=(job_id, node, target_node, clone_vmid, result, agent_enabled),
            daemon=True,
        ).start()

        return jsonify(
            {
                "success": True,
                "message": f"VM {vmid} cloned successfully as {clone_vmid}",
                "new_vmid": clone_vmid,
                "target_node": target_node,
                "task": result,
                "job_id": job_id,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<node>/<vmid>/delete", methods=["POST"])
def api_vm_delete(node, vmid):
    """API endpoint to delete a VM or container"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        # Detect if this is a QEMU VM or LXC container
        vm_type = None
        vm_status = None
        vm_name = vmid

        # Try QEMU first
        try:
            vm_status = proxmox.nodes(node).qemu(vmid).status.current.get()
            vm_type = "qemu"
            try:
                vm_config = proxmox.nodes(node).qemu(vmid).config.get()
                vm_name = vm_config.get("name", vmid)
            except Exception:
                pass
        except Exception:
            pass

        # Try LXC if QEMU failed
        if vm_type is None:
            try:
                vm_status = proxmox.nodes(node).lxc(vmid).status.current.get()
                vm_type = "lxc"
                try:
                    vm_config = proxmox.nodes(node).lxc(vmid).config.get()
                    vm_name = vm_config.get("hostname", vmid)
                except Exception:
                    pass
            except Exception:
                pass

        if vm_type is None:
            return jsonify({"error": f"VM/Container {vmid} does not exist"}), 404

        # Check if VM/container is stopped
        if vm_status.get("status") != "stopped":
            return (
                jsonify(
                    {
                        "error": f"{'Container' if vm_type == 'lxc' else 'VM'} must be stopped before deletion. Current status: {vm_status.get('status')}"
                    }
                ),
                400,
            )

        # Delete the VM or container
        if vm_type == "lxc":
            result = proxmox.nodes(node).lxc(vmid).delete(purge=1)
            type_label = "Container"
        else:
            result = proxmox.nodes(node).qemu(vmid).delete(purge=1)
            type_label = "VM"

        return jsonify(
            {
                "success": True,
                "message": f"{type_label} {vm_name} (ID: {vmid}) has been deleted successfully",
                "task": result,
            }
        )

    except Exception as e:
        error_msg = str(e)

        # Check for common error patterns
        if "does not exist" in error_msg.lower():
            return jsonify({"error": f"VM/Container {vmid} does not exist"}), 404
        elif "not stopped" in error_msg.lower() or "running" in error_msg.lower():
            return (
                jsonify({"error": "VM/Container must be stopped before deletion"}),
                400,
            )
        else:
            return jsonify({"error": f"Failed to delete: {error_msg}"}), 500


# =============================================================================
# VNC Proxy Endpoints
# =============================================================================


@app.route("/api/vm/<node>/<vmid>/vnc-ticket", methods=["POST"])
def api_vnc_ticket(node, vmid):
    """
    Get a VNC ticket from Proxmox and create a proxy session.
    Returns a session ID that can be used to connect via WebSocket.
    Supports both QEMU VMs and LXC containers.
    """
    cleanup_expired_vnc_sessions()

    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    # Determine if this is a QEMU VM or LXC container
    vm_type = "qemu"
    try:
        proxmox.nodes(node).qemu(vmid).status.current.get()
    except:
        vm_type = "lxc"

    # Get connection metadata for this node to get the host
    metadata = connection_metadata.get(node)
    if not metadata:
        # Debug: show all available metadata keys
        print(f"Available connection_metadata keys: {list(connection_metadata.keys())}")
        return (
            jsonify({"error": f"Connection metadata not found for node '{node}'"}),
            500,
        )

    try:
        # Request VNC ticket from Proxmox (both use vncproxy with websocket=1)
        if vm_type == "qemu":
            vnc_data = proxmox.nodes(node).qemu(vmid).vncproxy.post(websocket=1)
        else:
            vnc_data = proxmox.nodes(node).lxc(vmid).vncproxy.post(websocket=1)

        # Create a session for this VNC connection
        session_id = str(uuid.uuid4())

        # Prepare auth info for WebSocket connection
        auth_token = None
        auth_ticket = None

        if metadata.get("token_name") and metadata.get("token_value"):
            # API token authentication
            auth_token = f"PVEAPIToken={metadata['user']}!{metadata['token_name']}={metadata['token_value']}"
        else:
            # Password auth - we need to get a fresh ticket or use the existing one
            # For password auth, let's request a new access ticket
            try:
                # Get auth ticket by making a direct API call to get access
                host = metadata["host"]
                user = metadata["user"]
                password = metadata.get("password")
                verify_ssl = metadata.get("verify_ssl", True)

                # Request access ticket from Proxmox
                import requests as req_lib

                ticket_url = f"https://{host}:8006/api2/json/access/ticket"
                ticket_response = req_lib.post(
                    ticket_url,
                    data={"username": user, "password": password},
                    verify=verify_ssl,
                    timeout=10,
                )
                if ticket_response.status_code == 200:
                    ticket_data = ticket_response.json().get("data", {})
                    auth_ticket = ticket_data.get("ticket")
            except Exception:
                pass  # Auth ticket retrieval failed, will try without

        with vnc_sessions_lock:
            vnc_sessions[session_id] = {
                "ticket": vnc_data["ticket"],
                "port": vnc_data["port"],
                "node": node,
                "vmid": vmid,
                "vm_type": vm_type,
                "created_at": time.time(),
                "proxmox_host": metadata["host"],
                "verify_ssl": metadata.get("verify_ssl", True),
                "auth_token": auth_token,
                "auth_ticket": auth_ticket,
            }

        return jsonify(
            {
                "success": True,
                "session_id": session_id,
                "websocket_url": f"/vnc/{session_id}",
                "password": vnc_data["ticket"],  # VNC password for noVNC
            }
        )

    except Exception as e:
        error_msg = str(e)
        if "not running" in error_msg.lower():
            return jsonify({"error": "VM must be running to access console"}), 400
        return jsonify({"error": f"Failed to get VNC ticket: {error_msg}"}), 500


@sock.route("/vnc/<session_id>")
def vnc_websocket_proxy(ws, session_id):
    """
    WebSocket proxy that relays traffic between browser (noVNC) and Proxmox VNC.
    """
    # Get session data
    with vnc_sessions_lock:
        session = vnc_sessions.get(session_id)
        if not session:
            ws.close(1008, "Invalid or expired session")
            return

    # Build Proxmox WebSocket URL
    proxmox_host = session["proxmox_host"]
    port = session["port"]
    ticket = session["ticket"]
    node = session["node"]
    vmid = session["vmid"]
    vm_type = session.get("vm_type", "qemu")
    verify_ssl = session["verify_ssl"]
    auth_ticket = session.get("auth_ticket")
    auth_token = session.get("auth_token")

    # URL-encode the VNC ticket properly
    from urllib.parse import quote

    encoded_ticket = quote(ticket, safe="")

    # Proxmox WebSocket URL format (both use vncwebsocket)
    proxmox_ws_url = f"wss://{proxmox_host}:8006/api2/json/nodes/{node}/{vm_type}/{vmid}/vncwebsocket?port={port}&vncticket={encoded_ticket}"

    # SSL context
    ssl_opts = {}
    if not verify_ssl:
        ssl_opts["sslopt"] = {
            "cert_reqs": ssl.CERT_NONE,
            "check_hostname": False,
        }

    # Build headers for authentication
    headers = {}
    if auth_token:
        # API token authentication
        headers["Authorization"] = auth_token
    elif auth_ticket:
        # Cookie-based authentication
        headers["Cookie"] = f"PVEAuthCookie={quote(auth_ticket, safe='')}"

    proxmox_ws = None
    try:
        # Connect to Proxmox WebSocket
        proxmox_ws = websocket.create_connection(
            proxmox_ws_url,
            timeout=30,
            header=headers if headers else None,
            **ssl_opts,
        )

        # Set up bidirectional relay using threads
        stop_event = threading.Event()

        def relay_to_browser():
            """Relay data from Proxmox to browser"""
            try:
                proxmox_ws.settimeout(1.0)
                while not stop_event.is_set():
                    try:
                        opcode, data = proxmox_ws.recv_data()
                        if data:
                            # Send as binary if it's binary data
                            if opcode == websocket.ABNF.OPCODE_BINARY:
                                ws.send(data)
                            else:
                                ws.send(
                                    data.decode("utf-8")
                                    if isinstance(data, bytes)
                                    else data
                                )
                        else:
                            break
                    except websocket.WebSocketTimeoutException:
                        continue
                    except Exception:
                        break
            finally:
                stop_event.set()

        def relay_to_proxmox():
            """Relay data from browser to Proxmox"""
            from simple_websocket import ConnectionClosed

            try:
                while not stop_event.is_set():
                    try:
                        # flask-sock's receive() - use longer timeout
                        data = ws.receive(timeout=30)
                        if data is not None:
                            # Send binary data as binary
                            if isinstance(data, bytes):
                                proxmox_ws.send_binary(data)
                            else:
                                proxmox_ws.send(data)
                        # None on timeout is OK, just continue
                    except ConnectionClosed:
                        break
                    except TimeoutError:
                        # Timeout is normal, just continue waiting
                        continue
                    except Exception as e:
                        error_str = str(e).lower()
                        if "timed out" in error_str or "timeout" in error_str:
                            continue
                        # Check for connection closed
                        if "closed" in error_str or "disconnect" in error_str:
                            break
                        break
            finally:
                stop_event.set()

        # Start relay threads
        browser_thread = threading.Thread(target=relay_to_browser, daemon=True)
        proxmox_thread = threading.Thread(target=relay_to_proxmox, daemon=True)

        browser_thread.start()
        proxmox_thread.start()

        # Wait for either thread to finish
        while not stop_event.is_set():
            time.sleep(0.1)

    except Exception:
        pass  # Connection error, silently close
    finally:
        if proxmox_ws:
            try:
                proxmox_ws.close()
            except Exception:
                pass

        # Clean up session
        with vnc_sessions_lock:
            vnc_sessions.pop(session_id, None)


@app.route("/vm/<node>/<vmid>/console")
def vm_console(node, vmid):
    """VNC console page for a VM or Container"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        flash("Proxmox connection not available")
        return redirect(url_for("index"))

    try:
        # Determine if this is a QEMU VM or LXC container
        vm_type = "qemu"
        try:
            vm_status = proxmox.nodes(node).qemu(vmid).status.current.get()
            vm_config = proxmox.nodes(node).qemu(vmid).config.get()
        except:
            vm_type = "lxc"
            vm_status = proxmox.nodes(node).lxc(vmid).status.current.get()
            vm_config = proxmox.nodes(node).lxc(vmid).config.get()

        vm_name = vm_config.get("name") or vm_config.get("hostname") or f"VM {vmid}"

        return render_template(
            "vm_console.html",
            node=node,
            vmid=vmid,
            vm_name=vm_name,
            vm_status=vm_status.get("status", "unknown"),
            vm_type=vm_type,
        )
    except Exception as e:
        flash(f"Error loading console: {str(e)}")
        return redirect(url_for("vm_detail", node=node, vmid=vmid))


@app.route("/api/cloud-images")
def api_cloud_images():
    """API endpoint to get available cloud images for template creation"""
    images = []
    for image_id, image_info in CLOUD_IMAGES.items():
        images.append(
            {
                "id": image_id,
                "name": image_info["name"],
                "description": image_info["description"],
                "url": image_info["url"],
            }
        )
    return jsonify(images)


# =============================================================================
# Job Queue API Endpoints
# =============================================================================


@app.route("/api/jobs")
def api_get_jobs():
    """Get all jobs"""
    jobs = job_queue.get_all_jobs()
    return jsonify(jobs)


@app.route("/api/jobs/<job_id>")
def api_get_job(job_id):
    """Get a specific job"""
    job = job_queue.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def api_delete_job(job_id):
    """Delete a job"""
    job = job_queue.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] == "running":
        return jsonify({"error": "Cannot delete a running job"}), 400
    job_queue.delete_job(job_id)
    return jsonify({"success": True})


# =============================================================================
# Cloud Template Creation with Background Jobs
# =============================================================================


def wait_for_task(proxmox, node, task_upid, job_id, timeout=600, poll_interval=3):
    """Wait for a Proxmox task to complete, updating job progress"""
    waited = 0
    while waited < timeout:
        try:
            task_status = proxmox.nodes(node).tasks(task_upid).status.get()
            status = task_status.get("status")
            if status == "stopped":
                exitstatus = task_status.get("exitstatus", "")
                if exitstatus == "OK":
                    return {"success": True}
                else:
                    return {"success": False, "error": f"Task failed: {exitstatus}"}
        except Exception as e:
            job_queue.add_step(job_id, f"Error checking task status: {e}")

        time.sleep(poll_interval)
        waited += poll_interval

    return {"success": False, "error": "Task timed out"}


def run_clone_job(
    job_id, source_node, target_node, target_vmid, task_upid, agent_enabled
):
    """Background job tracking a VM clone: wait for the disk clone task (which
    Proxmox files under the source node), then apply post-clone configuration
    (the guest-agent toggle) on the target once its lock is released.
    """
    job_queue.set_running(job_id)
    job_queue.add_step(job_id, "Cloning disk…")
    job_queue.update_job(job_id, progress=10)

    try:
        proxmox = get_proxmox_connection(source_node, auto_renew=True)
        if not proxmox:
            job_queue.set_failed(job_id, "Failed to connect to source node")
            return

        # The clone task lives on the source node.
        result = wait_for_task(
            proxmox, source_node, task_upid, job_id, timeout=7200, poll_interval=5
        )
        if not result.get("success"):
            job_queue.set_failed(job_id, result.get("error", "Clone task failed"))
            return

        job_queue.add_step(job_id, "Disk clone finished")
        job_queue.update_job(job_id, progress=80)

        # Configuring phase: apply the guest-agent toggle on the target,
        # preserving any extra agent options the clone inherited.
        job_queue.add_step(job_id, "Applying configuration (guest agent)…")
        conn = get_proxmox_connection(target_node, auto_renew=True) or proxmox
        try:
            cfg = conn.nodes(target_node).qemu(target_vmid).config.get()
            extra = [p for p in str(cfg.get("agent", "")).split(",")[1:] if p.strip()]
        except Exception:
            extra = []
        flag = "1" if agent_enabled else "0"
        agent_val = ",".join([flag] + extra) if extra else flag
        conn.nodes(target_node).qemu(target_vmid).config.put(agent=agent_val)

        job_queue.set_completed(
            job_id, result={"vmid": target_vmid, "node": target_node}
        )
    except Exception as e:
        job_queue.set_failed(job_id, str(e))


def run_cloud_template_job(job_id, node, params):
    """Background job to create a cloud template via Proxmox API"""
    job_queue.set_running(job_id)

    try:
        # Get connection
        proxmox = get_proxmox_connection(node, auto_renew=True)
        if not proxmox:
            job_queue.set_failed(job_id, "Failed to connect to node")
            return

        image_id = params["image_id"]
        image_info = CLOUD_IMAGES[image_id]
        image_url = image_info["url"]
        if not is_allowed_download_url(image_url):
            job_queue.set_failed(job_id, "Cloud image URL must be http:// or https://")
            return

        image_filename_original = filename_from_url(image_url)

        # Normalize filename extension for Proxmox import
        # Cloud images with .img extension are typically qcow2 format
        # Proxmox download-url API requires proper extension for 'import' content type
        if image_filename_original.endswith(".img"):
            image_filename = image_filename_original[:-4] + ".qcow2"
        elif not any(
            image_filename_original.endswith(ext) for ext in [".qcow2", ".raw", ".vmdk"]
        ):
            # Add .qcow2 extension if no recognized extension
            image_filename = image_filename_original + ".qcow2"
        else:
            image_filename = image_filename_original

        if not is_safe_download_filename(
            image_filename, (".img", ".qcow2", ".raw", ".vmdk")
        ):
            job_queue.set_failed(job_id, "Cloud image filename is invalid")
            return

        storage = params["storage"]
        vmid = params["vmid"]
        name = params["name"]
        cores = params["cores"]
        memory = params["memory"]
        disk_size = params["disk_size"]
        bridge = params["bridge"]
        ci_user = params.get("ci_user", "")
        ci_password = params.get("ci_password", "")
        ci_sshkeys = params.get("ci_sshkeys", "")
        ip_config = params.get("ip_config", "ip=dhcp,ip6=auto")

        # Step 1: Find storage for downloading (needs 'import' or 'images' content type)
        job_queue.add_step(job_id, "Finding storage for image download...")
        job_queue.update_job(job_id, progress=5)

        storages = proxmox.nodes(node).storage.get()
        download_storage = None
        download_content_type = None

        # Priority: 1) storage with 'import' content, 2) target storage if it supports images
        for s in storages:
            if s.get("active", 0) != 1:
                continue
            content = s.get("content", "").split(",")

            # Check for 'import' content type (Proxmox 8.1+)
            if "import" in content:
                download_storage = s.get("storage")
                download_content_type = "import"
                break

        # If no 'import' storage, check if target storage supports images
        if not download_storage:
            for s in storages:
                if s.get("storage") == storage and s.get("active", 0) == 1:
                    content = s.get("content", "").split(",")
                    if "images" in content:
                        download_storage = storage
                        download_content_type = "import"  # Try import content type
                        break

        # Fallback: any storage with images content
        if not download_storage:
            for s in storages:
                if s.get("active", 0) != 1:
                    continue
                content = s.get("content", "").split(",")
                if "images" in content:
                    download_storage = s.get("storage")
                    download_content_type = "import"
                    break

        if not download_storage:
            job_queue.set_failed(
                job_id,
                "No storage with 'import' or 'images' content type found. Proxmox 8.1+ required for cloud template import.",
            )
            return

        job_queue.add_step(
            job_id,
            f"Using storage '{download_storage}' for download (content type: {download_content_type})",
        )

        # Step 2: Check if image already exists and handle according to cache mode
        job_queue.add_step(job_id, f"Checking for existing image: {image_filename}...")
        job_queue.update_job(job_id, progress=10)

        image_volid = f"{download_storage}:{download_content_type}/{image_filename}"
        image_exists = False
        skip_download = False

        try:
            # Check if image already exists in storage
            storage_content = (
                proxmox.nodes(node).storage(download_storage).content.get()
            )
            for item in storage_content:
                if item.get("volid") == image_volid:
                    image_exists = True
                    break
        except Exception as e:
            job_queue.add_step(
                job_id, f"Warning: Could not check existing content: {e}"
            )

        if image_exists:
            if CLOUD_IMAGE_CACHE_MODE == "REUSE":
                job_queue.add_step(
                    job_id,
                    "Image already exists, reusing cached file (CLOUD_IMAGE_CACHE=REUSE)",
                )
                skip_download = True
            else:
                # OVERWRITE mode - delete existing file first
                job_queue.add_step(
                    job_id,
                    "Image exists, deleting for fresh download (CLOUD_IMAGE_CACHE=OVERWRITE)",
                )
                try:
                    proxmox.nodes(node).storage(download_storage).content(
                        image_volid
                    ).delete()
                    job_queue.add_step(job_id, "Existing image deleted")
                except Exception as e:
                    job_queue.set_failed(
                        job_id,
                        f"Failed to delete existing image: {e}. Try setting CLOUD_IMAGE_CACHE=REUSE",
                    )
                    return

        if not skip_download:
            # Download cloud image to the storage
            job_queue.add_step(job_id, f"Downloading cloud image: {image_filename}...")

            try:
                download_task = (
                    proxmox.nodes(node)
                    .storage(download_storage)("download-url")
                    .post(
                        content=download_content_type,
                        filename=image_filename,
                        url=image_url,
                    )
                )
                job_queue.add_step(job_id, f"Download task started: {download_task}")
            except Exception as e:
                error_str = str(e).lower()
                if "import" in error_str or "content" in error_str:
                    job_queue.set_failed(
                        job_id,
                        f"Storage does not support 'import' content type. Proxmox 8.1+ required. Error: {e}",
                    )
                else:
                    job_queue.set_failed(job_id, f"Failed to start download: {e}")
                return

            # Wait for download to complete
            job_queue.add_step(
                job_id,
                "Waiting for download to complete (this may take several minutes)...",
            )
            result = wait_for_task(
                proxmox, node, download_task, job_id, timeout=900
            )  # 15 min timeout
            if not result["success"]:
                job_queue.set_failed(job_id, result["error"])
                return

        job_queue.add_step(job_id, "Download completed successfully")
        job_queue.update_job(job_id, progress=40)

        # Step 3: Create VM
        job_queue.add_step(job_id, f"Creating VM {vmid}...")

        vm_config = {
            "vmid": vmid,
            "name": name,
            "memory": memory,
            "cores": cores,
            "ostype": image_info.get("os_type", "l26"),
            "scsihw": "virtio-scsi-pci",
            "net0": f"virtio,bridge={bridge}",
            "serial0": "socket",  # Keep serial console for troubleshooting
            "vga": "qxl",  # SPICE display
            "agent": "enabled=1",
        }

        try:
            proxmox.nodes(node).qemu.create(**vm_config)
            job_queue.add_step(job_id, f"VM {vmid} created")
        except Exception as e:
            job_queue.set_failed(job_id, f"Failed to create VM: {e}")
            return

        job_queue.update_job(job_id, progress=50)

        # Step 4: Import disk using import-from parameter
        job_queue.add_step(job_id, "Importing disk from cloud image...")

        # The downloaded image is at: {storage}:import/{filename}
        import_path = f"{download_storage}:{download_content_type}/{image_filename}"
        job_queue.add_step(job_id, f"Import path: {import_path}")

        try:
            # Use POST (async) instead of PUT (sync) for disk import - this is a slow operation
            import_task = (
                proxmox.nodes(node)
                .qemu(vmid)
                .config.post(scsi0=f"{storage}:0,import-from={import_path}")
            )
            job_queue.add_step(job_id, f"Disk import task started: {import_task}")

            # Wait for import task to complete (can take several minutes for large images)
            result = wait_for_task(proxmox, node, import_task, job_id, timeout=600)
            if not result["success"]:
                job_queue.set_failed(job_id, f"Disk import failed: {result['error']}")
                return
            job_queue.add_step(job_id, "Disk imported successfully")
        except Exception as e:
            job_queue.set_failed(job_id, f"Failed to import disk: {e}")
            return

        job_queue.update_job(job_id, progress=65)

        # Step 5: Configure boot disk (simple config change, PUT is fine)
        job_queue.add_step(job_id, "Configuring boot settings...")
        try:
            proxmox.nodes(node).qemu(vmid).config.put(
                boot="order=scsi0", bootdisk="scsi0"
            )
        except Exception as e:
            job_queue.add_step(job_id, f"Boot config warning: {e}")

        job_queue.update_job(job_id, progress=70)

        # Step 6: Add cloud-init drive (uses storage allocation, use async POST)
        job_queue.add_step(job_id, "Adding cloud-init drive...")
        try:
            cloudinit_task = (
                proxmox.nodes(node).qemu(vmid).config.post(ide2=f"{storage}:cloudinit")
            )
            job_queue.add_step(job_id, f"Cloud-init drive task: {cloudinit_task}")
            result = wait_for_task(proxmox, node, cloudinit_task, job_id, timeout=120)
            if not result["success"]:
                job_queue.add_step(
                    job_id, f"Cloud-init drive warning: {result['error']}"
                )
        except Exception as e:
            job_queue.add_step(job_id, f"Cloud-init drive warning: {e}")

        job_queue.update_job(job_id, progress=75)

        # Step 7: Configure cloud-init settings
        job_queue.add_step(job_id, "Configuring cloud-init...")
        ci_config = {"ipconfig0": ip_config}
        if ci_user:
            ci_config["ciuser"] = ci_user
        if ci_password:
            ci_config["cipassword"] = ci_password
        if ci_sshkeys:
            # SSH keys need to be URL-encoded
            import urllib.parse

            ci_config["sshkeys"] = urllib.parse.quote(ci_sshkeys, safe="")

        try:
            proxmox.nodes(node).qemu(vmid).config.put(**ci_config)
        except Exception as e:
            job_queue.add_step(job_id, f"Cloud-init config warning: {e}")

        job_queue.update_job(job_id, progress=80)

        # Step 8: Resize disk (storage operation, but resize endpoint only supports PUT)
        # Using retry mechanism since this can be slow
        job_queue.add_step(job_id, f"Resizing disk to {disk_size}...")
        try:

            def do_disk_resize():
                return (
                    proxmox.nodes(node)
                    .qemu(vmid)
                    .resize.put(disk="scsi0", size=disk_size)
                )

            retry_on_timeout(
                do_disk_resize,
                max_attempts=5,
                base_delay=3,
                job_id=job_id,
                job_queue_ref=job_queue,
                operation_name="disk resize",
            )
            job_queue.add_step(job_id, "Disk resized successfully")
        except Exception as e:
            job_queue.add_step(job_id, f"Disk resize warning: {e}")

        job_queue.update_job(job_id, progress=90)

        # Step 9: Convert to template (already async POST)
        job_queue.add_step(job_id, "Converting VM to template...")
        try:
            template_task = proxmox.nodes(node).qemu(vmid).template.post()
            if template_task:
                job_queue.add_step(job_id, f"Template conversion task: {template_task}")
                result = wait_for_task(
                    proxmox, node, template_task, job_id, timeout=120
                )
                if not result["success"]:
                    job_queue.add_step(
                        job_id, f"Template conversion warning: {result['error']}"
                    )
                else:
                    job_queue.add_step(job_id, "VM converted to template")
            else:
                job_queue.add_step(job_id, "VM converted to template")
        except Exception as e:
            job_queue.add_step(job_id, f"Template conversion warning: {e}")

        job_queue.update_job(job_id, progress=95)

        # Step 10: Cleanup - delete the downloaded image (unless using cached image in REUSE mode)
        if skip_download and CLOUD_IMAGE_CACHE_MODE == "REUSE":
            job_queue.add_step(
                job_id,
                "Keeping cached image for future use (CLOUD_IMAGE_CACHE=REUSE)",
            )
        else:
            job_queue.add_step(job_id, "Cleaning up temporary files...")
            try:
                # Delete the downloaded image file
                content_id = (
                    f"{download_storage}:{download_content_type}/{image_filename}"
                )

                def do_cleanup():
                    proxmox.nodes(node).storage(download_storage).content(
                        content_id
                    ).delete()

                retry_on_timeout(
                    do_cleanup,
                    job_id=job_id,
                    job_queue_ref=job_queue,
                    operation_name="cleanup",
                )
                job_queue.add_step(job_id, "Temporary image deleted")
            except Exception as e:
                job_queue.add_step(
                    job_id,
                    f"Cleanup warning: {e} (you may want to manually delete {image_filename} from {download_storage})",
                )

        # Done!
        job_queue.set_completed(
            job_id,
            {
                "vmid": vmid,
                "name": name,
                "node": node,
            },
        )

    except Exception as e:
        job_queue.set_failed(job_id, str(e))


@app.route("/api/node/<node>/create-cloud-template", methods=["POST"])
def api_create_cloud_template(node):
    """API endpoint to create a VM template from a cloud image (background job)"""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        # Required parameters
        image_id = data.get("image_id")
        storage = data.get("storage")

        if not image_id or not storage:
            return (
                jsonify({"error": "Missing required parameters: image_id, storage"}),
                400,
            )

        if image_id not in CLOUD_IMAGES:
            return jsonify({"error": f"Unknown cloud image: {image_id}"}), 400

        # Get next VMID if not provided
        vmid = data.get("vmid")
        if not vmid:
            try:
                vmid = proxmox.cluster.nextid.get()
            except Exception:
                return jsonify({"error": "Failed to get next available VMID"}), 500

        vmid = int(vmid)

        # Prepare job parameters
        params = {
            "image_id": image_id,
            "storage": storage,
            "vmid": vmid,
            "name": data.get("name", f"{image_id}-template"),
            "cores": int(data.get("cores", 2)),
            "memory": int(data.get("memory", 2048)),
            "disk_size": data.get("disk_size", "10G"),
            "bridge": data.get("bridge", "vmbr0"),
            "ci_user": data.get("ci_user", ""),
            "ci_password": data.get("ci_password", ""),
            "ci_sshkeys": data.get("ci_sshkeys", ""),
            "ip_config": data.get("ip_config", "ip=dhcp,ip6=auto"),
        }

        # Create job
        job_id = job_queue.create_job(
            job_type="create_cloud_template",
            description=f"Create template '{params['name']}' on {node}",
            params=params,
        )

        # Start background thread
        thread = threading.Thread(
            target=run_cloud_template_job, args=(job_id, node, params), daemon=True
        )
        thread.start()

        return jsonify(
            {
                "success": True,
                "message": "Template creation job started",
                "job_id": job_id,
                "vmid": vmid,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── LXC Advanced Configuration Routes ───────────────────────────────────────


@app.route("/api/vm/<node>/<vmid>/lxc/features", methods=["GET", "PUT"])
def api_lxc_features(node, vmid):
    """Get or update LXC container feature flags (nesting, keyctl, fuse, mount, mknod)."""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        config = proxmox.nodes(node).lxc(vmid).config.get()
        status = proxmox.nodes(node).lxc(vmid).status.current.get()
        is_running = status.get("status") == "running"

        if request.method == "GET":
            features_str = config.get("features", "")
            can_write = check_lxc_write_permission(proxmox, vmid, node)
            backup_key = f"{node}:{vmid}"
            backup = lxc_config_backups.get(backup_key)
            return jsonify(
                {
                    "features": parse_lxc_features(features_str),
                    "features_raw": features_str,
                    "running": is_running,
                    "can_write": can_write,
                    "has_backup": backup is not None,
                    "backup_timestamp": backup["timestamp"] if backup else None,
                }
            )

        # PUT — update features
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        _backup_lxc_config(node, vmid, config)

        new_features_str = serialize_lxc_features(data.get("features", {}))
        params = {}
        if new_features_str:
            params["features"] = new_features_str
        else:
            params["delete"] = "features"

        proxmox.nodes(node).lxc(vmid).config.put(**params)

        msg = "Features updated."
        if is_running:
            msg += " Restart the container for changes to take effect."

        return jsonify(
            {
                "success": True,
                "message": msg,
                "features_raw": new_features_str,
                "restart_required": is_running,
            }
        )

    except Exception as e:
        traceback.print_exc()
        return _proxmox_error_response(e)


@app.route("/api/vm/<node>/<vmid>/lxc/config-snapshot", methods=["GET"])
def api_lxc_config_snapshot(node, vmid):
    """Return the last config snapshot taken before a write for this container."""
    backup_key = f"{node}:{vmid}"
    backup = lxc_config_backups.get(backup_key)
    if not backup:
        return jsonify({"error": "No snapshot available"}), 404
    return jsonify(
        {
            "timestamp": backup["timestamp"],
            "features": backup.get("features", ""),
        }
    )


@app.route("/api/vm/<node>/<vmid>/lxc/config-restore", methods=["POST"])
def api_lxc_config_restore(node, vmid):
    """Restore all LXC advanced config keys from the pre-write snapshot."""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    backup_key = f"{node}:{vmid}"
    backup = lxc_config_backups.get(backup_key)
    if not backup:
        return jsonify({"error": "No snapshot available"}), 404

    try:
        current_config = proxmox.nodes(node).lxc(vmid).config.get()
        backup_config = backup["config"]
        _restore_advanced_keys(proxmox, node, vmid, backup_config, current_config)
        del lxc_config_backups[backup_key]

        return jsonify(
            {
                "success": True,
                "message": f"Config restored to snapshot from {backup['timestamp']}.",
                "features_raw": backup_config.get("features", ""),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<node>/<vmid>/lxc/devices", methods=["GET", "POST"])
def api_lxc_devices(node, vmid):
    """List or add device passthrough entries for an LXC container."""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        config = proxmox.nodes(node).lxc(vmid).config.get()
        status = proxmox.nodes(node).lxc(vmid).status.current.get()
        is_running = status.get("status") == "running"
        pve_ver = get_pve_version_tuple(node)
        supports_dev_n = pve_ver >= (8, 2, 0)

        if request.method == "GET":
            # Enumerate dev[n] entries (PVE 8.2+)
            devices = []
            dev_pattern = re.compile(r"^dev(\d+)$")
            for key in sorted(config):
                m = dev_pattern.match(key)
                if m:
                    parsed = parse_lxc_dev(config[key])
                    parsed["key"] = key
                    parsed["type"] = "dev_n"
                    devices.append(parsed)

            # Enumerate lxc[n] raw entries (PVE < 8.2 or manual)
            legacy = []
            lxc_pattern = re.compile(r"^lxc(\d+)$")
            for key in sorted(config):
                if lxc_pattern.match(key):
                    raw = str(config[key])
                    if "lxc.cgroup2.devices.allow" in raw or "lxc.mount.entry" in raw:
                        legacy.append({"key": key, "raw": raw})

            can_write = check_lxc_write_permission(proxmox, vmid, node)
            return jsonify(
                {
                    "devices": devices,
                    "legacy_devices": legacy,
                    "pve_version": ".".join(str(x) for x in pve_ver),
                    "pve_supports_dev_n": supports_dev_n,
                    "running": is_running,
                    "can_write": can_write,
                }
            )

        # POST — add a device
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        if is_running:
            return (
                jsonify(
                    {
                        "error": "Container must be stopped before adding devices.",
                        "stop_required": True,
                    }
                ),
                409,
            )

        device_path = (data.get("path") or "").strip()
        if not device_path:
            return jsonify({"error": "Device path is required"}), 400

        _backup_lxc_config(node, vmid, config)

        if supports_dev_n:
            idx = _next_key_index(config, "dev")
            dev_value = serialize_lxc_dev(
                {
                    "path": device_path,
                    "uid": data.get("uid") or None,
                    "gid": data.get("gid") or None,
                    "mode": data.get("mode") or None,
                }
            )
            proxmox.nodes(node).lxc(vmid).config.put(**{f"dev{idx}": dev_value})
            return jsonify(
                {
                    "success": True,
                    "message": f"Device {device_path} added as dev{idx}.",
                    "key": f"dev{idx}",
                }
            )
        else:
            # PVE < 8.2: write cgroup2 allow + mount.entry via lxc[n] raw keys
            cgroup_info = KNOWN_LXC_DEVICE_CGROUPS.get(device_path)
            major = data.get("major")
            minor = data.get("minor")
            dev_type = data.get("dev_type", "c")

            if cgroup_info:
                dev_type, major, minor = cgroup_info
            elif major is None or minor is None:
                return (
                    jsonify(
                        {
                            "error": (
                                "PVE < 8.2 requires cgroup major:minor for unknown devices. "
                                "Provide major, minor, dev_type fields."
                            )
                        }
                    ),
                    400,
                )

            idx0 = _next_key_index(config, "lxc")
            # Container path strips leading /dev/ prefix
            ct_path = device_path.lstrip("/")
            params = {
                f"lxc{idx0}": f"lxc.cgroup2.devices.allow = {dev_type} {major}:{minor} rwm",
                f"lxc{idx0 + 1}": (
                    f"lxc.mount.entry = {device_path} {ct_path} "
                    "none bind,optional,create=file"
                ),
            }
            proxmox.nodes(node).lxc(vmid).config.put(**params)
            return jsonify(
                {
                    "success": True,
                    "message": f"Device {device_path} added via cgroup2 (PVE < 8.2 mode).",
                    "key": f"lxc{idx0}",
                }
            )

    except Exception as e:
        return _proxmox_error_response(e)


@app.route("/api/vm/<node>/<vmid>/lxc/devices/<key>", methods=["DELETE"])
def api_lxc_device_delete(node, vmid, key):
    """Remove a device passthrough entry by config key."""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        config = proxmox.nodes(node).lxc(vmid).config.get()
        status = proxmox.nodes(node).lxc(vmid).status.current.get()

        if status.get("status") == "running":
            return (
                jsonify(
                    {
                        "error": "Container must be stopped before removing devices.",
                        "stop_required": True,
                    }
                ),
                409,
            )

        if key not in config:
            return jsonify({"error": f"Key '{key}' not found in container config"}), 404

        _backup_lxc_config(node, vmid, config)

        # For lxc[n] mount.entry lines, also remove the paired cgroup2 allow line
        delete_keys = [key]
        lxc_pattern = re.compile(r"^lxc\d+$")
        if lxc_pattern.match(key) and "lxc.mount.entry" in str(config.get(key, "")):
            mount_val = str(config[key])
            # Find the cgroup2 allow line that was written alongside this entry
            for k, v in config.items():
                if (
                    k != key
                    and lxc_pattern.match(k)
                    and "lxc.cgroup2.devices.allow" in str(v)
                ):
                    # Heuristic: assume cgroup2 allow directly precedes this mount.entry
                    # (they are always written as a pair)
                    # A more robust check would track pairs by index proximity
                    try:
                        if abs(int(key[3:]) - int(k[3:])) == 1:
                            delete_keys.append(k)
                    except ValueError:
                        pass

        proxmox.nodes(node).lxc(vmid).config.put(delete=",".join(delete_keys))
        return jsonify({"success": True, "message": f"Device entry {key} removed."})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<node>/<vmid>/lxc/mounts", methods=["GET", "POST"])
def api_lxc_mounts(node, vmid):
    """List or add bind mount entries (mp[n]) for an LXC container."""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        config = proxmox.nodes(node).lxc(vmid).config.get()
        status = proxmox.nodes(node).lxc(vmid).status.current.get()
        is_running = status.get("status") == "running"

        if request.method == "GET":
            mounts = []
            mp_pattern = re.compile(r"^mp(\d+)$")
            for key in sorted(config):
                if mp_pattern.match(key) and is_lxc_bind_mount(config[key]):
                    parsed = parse_lxc_mp(config[key])
                    parsed["key"] = key
                    mounts.append(parsed)
            can_write = check_lxc_write_permission(proxmox, vmid, node)
            is_root = _check_is_root(node)
            return jsonify(
                {
                    "mounts": mounts,
                    "running": is_running,
                    "can_write": can_write,
                    "is_root": is_root,
                }
            )

        # POST — add a bind mount
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        if is_running:
            return (
                jsonify(
                    {
                        "error": "Container must be stopped before adding mounts.",
                        "stop_required": True,
                    }
                ),
                409,
            )

        host_path = (data.get("host_path") or "").strip()
        mp = (data.get("mp") or "").strip()
        if not host_path or not mp:
            return (
                jsonify({"error": "host_path and mp (container path) are required"}),
                400,
            )
        if not host_path.startswith("/"):
            return (
                jsonify(
                    {"error": "host_path must be an absolute path starting with /"}
                ),
                400,
            )

        _backup_lxc_config(node, vmid, config)

        idx = _next_key_index(config, "mp")
        mp_value = serialize_lxc_mp(
            {
                "host_path": host_path,
                "mp": mp,
                "ro": "1" if data.get("ro") else "0",
                "backup": "1" if data.get("backup") else "0",
            }
        )
        if not _check_is_root(node):
            return (
                jsonify(
                    {
                        "error": "Proxmox only allows root@pam to add bind mounts via API. "
                        "Connect as root@pam to use this feature."
                    }
                ),
                403,
            )

        proxmox.nodes(node).lxc(vmid).config.put(**{f"mp{idx}": mp_value})
        return jsonify(
            {
                "success": True,
                "message": f"Mount {host_path} → {mp} added as mp{idx}.",
                "key": f"mp{idx}",
            }
        )

    except Exception as e:
        return _proxmox_error_response(e)


@app.route("/api/vm/<node>/<vmid>/lxc/mounts/<key>", methods=["DELETE"])
def api_lxc_mount_delete(node, vmid, key):
    """Remove a bind mount entry by config key."""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        config = proxmox.nodes(node).lxc(vmid).config.get()
        status = proxmox.nodes(node).lxc(vmid).status.current.get()

        if status.get("status") == "running":
            return (
                jsonify(
                    {
                        "error": "Container must be stopped before removing mounts.",
                        "stop_required": True,
                    }
                ),
                409,
            )

        if key not in config:
            return jsonify({"error": f"Key '{key}' not found in container config"}), 404

        _backup_lxc_config(node, vmid, config)
        proxmox.nodes(node).lxc(vmid).config.put(delete=key)
        return jsonify({"success": True, "message": f"Mount {key} removed."})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<node>/<vmid>/lxc/idmap", methods=["GET", "POST"])
def api_lxc_idmap(node, vmid):
    """List or add lxc.idmap entries for an LXC container."""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        config = proxmox.nodes(node).lxc(vmid).config.get()
        status = proxmox.nodes(node).lxc(vmid).status.current.get()
        is_running = status.get("status") == "running"

        if request.method == "GET":
            idmaps = collect_lxc_idmaps(config)
            can_write = check_lxc_write_permission(proxmox, vmid, node)
            backup = lxc_config_backups.get(f"{node}:{vmid}")
            return jsonify(
                {
                    "idmaps": idmaps,
                    "running": is_running,
                    "can_write": can_write,
                    "has_backup": backup is not None,
                }
            )

        # POST — add an idmap entry
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        if is_running:
            return (
                jsonify(
                    {
                        "error": "Container must be stopped before modifying idmap.",
                        "stop_required": True,
                    }
                ),
                409,
            )

        id_type = data.get("type", "").strip()
        if id_type not in ("u", "g"):
            return jsonify({"error": "type must be 'u' (UID) or 'g' (GID)"}), 400

        try:
            ct_id = int(data["ct_id"])
            host_id = int(data["host_id"])
            count = int(data["count"])
        except (KeyError, ValueError, TypeError):
            return jsonify({"error": "ct_id, host_id, count must be integers"}), 400

        _backup_lxc_config(node, vmid, config)

        idx = _next_key_index(config, "lxc")
        raw_value = serialize_lxc_idmap_line(
            {"type": id_type, "ct_id": ct_id, "host_id": host_id, "count": count}
        )
        proxmox.nodes(node).lxc(vmid).config.put(**{f"lxc{idx}": raw_value})

        return jsonify(
            {
                "success": True,
                "message": f"idmap entry added: {raw_value}",
                "key": f"lxc{idx}",
                "raw": raw_value,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<node>/<vmid>/lxc/idmap/<key>", methods=["DELETE"])
def api_lxc_idmap_delete(node, vmid, key):
    """Remove a specific lxc.idmap entry by config key."""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        config = proxmox.nodes(node).lxc(vmid).config.get()
        status = proxmox.nodes(node).lxc(vmid).status.current.get()

        if status.get("status") == "running":
            return (
                jsonify(
                    {
                        "error": "Container must be stopped before removing idmap.",
                        "stop_required": True,
                    }
                ),
                409,
            )

        if key not in config:
            return jsonify({"error": f"Key '{key}' not found in container config"}), 404
        if "lxc.idmap" not in str(config[key]):
            return jsonify({"error": f"Key '{key}' is not an idmap entry"}), 400

        _backup_lxc_config(node, vmid, config)
        proxmox.nodes(node).lxc(vmid).config.put(delete=key)
        return jsonify({"success": True, "message": f"idmap entry {key} removed."})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/lxc-profiles", methods=["GET"])
def api_lxc_profiles():
    """Return all available LXC config profiles."""
    profiles = [
        {"id": pid, **{k: v for k, v in p.items()}} for pid, p in LXC_PROFILES.items()
    ]
    return jsonify({"profiles": profiles})


@app.route("/api/vm/<node>/<vmid>/lxc/profile-diff/<profile_id>", methods=["GET"])
def api_lxc_profile_diff(node, vmid, profile_id):
    """Compute the diff of applying a profile to the current container config."""
    if profile_id not in LXC_PROFILES:
        return jsonify({"error": f"Profile '{profile_id}' not found"}), 404

    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        config = proxmox.nodes(node).lxc(vmid).config.get()
        status = proxmox.nodes(node).lxc(vmid).status.current.get()
        profile = LXC_PROFILES[profile_id]
        diff = compute_profile_diff(profile, config)
        diff["running"] = status.get("status") == "running"
        diff["profile"] = {"id": profile_id, **profile}
        diff["pve_supports_dev_n"] = get_pve_version_tuple(node) >= (8, 2, 0)
        return jsonify(diff)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vm/<node>/<vmid>/lxc/profile-apply/<profile_id>", methods=["POST"])
def api_lxc_profile_apply(node, vmid, profile_id):
    """Apply a profile to an LXC container (additive — never removes existing config)."""
    if profile_id not in LXC_PROFILES:
        return jsonify({"error": f"Profile '{profile_id}' not found"}), 404

    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        config = proxmox.nodes(node).lxc(vmid).config.get()
        status = proxmox.nodes(node).lxc(vmid).status.current.get()
        is_running = status.get("status") == "running"
        profile = LXC_PROFILES[profile_id]
        diff = compute_profile_diff(profile, config)

        if not diff["changes_needed"]:
            return jsonify(
                {
                    "success": True,
                    "message": "Profile already fully applied — nothing to change.",
                }
            )

        if is_running and diff["devices_require_stop"]:
            return (
                jsonify(
                    {
                        "error": "Container must be stopped before applying device passthrough from this profile.",
                        "stop_required": True,
                    }
                ),
                409,
            )

        _backup_lxc_config(node, vmid, config)

        params = {}
        applied = []

        # Merge features (additive)
        if diff["features_to_add"]:
            current_features = parse_lxc_features(config.get("features", ""))
            for f in diff["features_to_add"]:
                k = f.split("=")[0]
                current_features[k] = "1"
            params["features"] = serialize_lxc_features(current_features)
            applied.append(f"features: {', '.join(diff['features_to_add'])}")

        # Add devices (additive, PVE 8.2+ only)
        pve_ver = get_pve_version_tuple(node)
        if diff["devices_to_add"]:
            if pve_ver < (8, 2, 0):
                return (
                    jsonify(
                        {"error": "Device passthrough requires PVE 8.2+ (dev[n] API)."}
                    ),
                    400,
                )
            working_config = dict(config)
            working_config.update(params)
            for dev in diff["devices_to_add"]:
                idx = _next_key_index(working_config, "dev")
                dev_val = serialize_lxc_dev(
                    {
                        "path": dev["path"],
                        "uid": dev.get("uid") or None,
                        "gid": dev.get("gid") or None,
                    }
                )
                key = f"dev{idx}"
                params[key] = dev_val
                working_config[key] = dev_val
                applied.append(f"{key}: {dev_val}")

        if params:
            proxmox.nodes(node).lxc(vmid).config.put(**params)

        msg = f"Profile '{profile['name']}' applied: {'; '.join(applied)}."
        if is_running and diff["features_to_add"]:
            msg += " Restart to activate feature changes."

        return jsonify({"success": True, "message": msg, "applied": applied})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── End LXC Advanced Routes ──────────────────────────────────────────────────


@app.errorhandler(404)
def not_found(error):
    return render_template("404.html"), 404


# ---------------------------------------------------------------------------
# Cloud-init helpers
# ---------------------------------------------------------------------------

# ───────────────────────────────────────────────────────────────────────────
# SSH / key management for snippet writes
#
# Proxmox has no API to upload snippets, so writing a custom cloud-init snippet
# to a node requires filesystem access. When ProxUI runs remotely we reach the
# node over SSH (Tier 2) or, if port 22 is blocked, the node Shell / termproxy
# over 8006 (Tier 3). ProxUI uses its own generated ed25519 key, installed once
# into root's cluster-shared authorized_keys.
# ───────────────────────────────────────────────────────────────────────────

_SSH_DIR = os.path.join(os.path.dirname(CONFIG_FILE_PATH), "ssh")
_SSH_KEY_PATH = os.path.join(_SSH_DIR, "proxui_ed25519")
_SSH_KEY_PUB_PATH = _SSH_KEY_PATH + ".pub"


class SnippetWriteError(Exception):
    """Raised when a snippet cannot be written to the node's storage."""


def _get_paramiko():
    """Lazy paramiko import; returns the module or None if not installed."""
    try:
        import paramiko

        return paramiko
    except ImportError:
        return None


def _ssh_key_comment() -> str:
    """Identifiable comment for ProxUI's public key, e.g. proxui-snippet@host-2026-07-11."""
    try:
        host = socket.gethostname() or "proxui"
    except Exception:
        host = "proxui"
    return f"proxui-snippet@{host}-{datetime.now().strftime('%Y-%m-%d')}"


def _ensure_ssh_key() -> tuple[str, str]:
    """Generate ProxUI's ed25519 keypair if missing. Returns (private_path, public_line).

    Uses the `cryptography` backend (a paramiko dependency) since
    paramiko.Ed25519Key has no generate() helper.
    """
    os.makedirs(_SSH_DIR, exist_ok=True)
    try:
        os.chmod(_SSH_DIR, 0o700)
    except OSError:
        pass
    if not os.path.exists(_SSH_KEY_PATH):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        priv = ed25519.Ed25519PrivateKey.generate()
        priv_bytes = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_b64 = (
            priv.public_key()
            .public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH,
            )
            .decode()
        )
        pub_line = f"{pub_b64} {_ssh_key_comment()}"
        # Write private key with restrictive perms before writing anything else.
        fd = os.open(_SSH_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(priv_bytes)
        with open(_SSH_KEY_PUB_PATH, "w") as f:
            f.write(pub_line + "\n")

    with open(_SSH_KEY_PUB_PATH) as f:
        return _SSH_KEY_PATH, f.read().strip()


def _ssh_public_key() -> str | None:
    """Return the public key line if the key exists, else None (without generating)."""
    try:
        with open(_SSH_KEY_PUB_PATH) as f:
            return f.read().strip()
    except OSError:
        return None


def _ssh_target_for_node(node: str) -> tuple[str | None, bool]:
    """Return (host, verify_ssl) to reach a node. Uses the configured API host.

    Writing to the cluster-shared pmxcfs storage only requires reaching any one
    node, so the configured host is sufficient even with a single API endpoint.
    """
    meta = connection_metadata.get(node) or {}
    return meta.get("host"), meta.get("verify_ssl", True)


def _ssh_connect_root(node: str, timeout: int = 20, host: str | None = None):
    """Open a paramiko SSHClient to `node` as root using ProxUI's key.

    `host` overrides the address to connect to — needed for node-specific
    commands (e.g. `pct exec`) since connection_metadata maps discovered nodes
    to the configured API endpoint, not their own address. Caller must close it.
    """
    paramiko = _get_paramiko()
    if not paramiko:
        raise SnippetWriteError("paramiko is not installed (pip install paramiko).")
    if host is None:
        host, _ = _ssh_target_for_node(node)
    if not host:
        raise SnippetWriteError(f"No connection host known for node '{node}'.")
    if not os.path.exists(_SSH_KEY_PATH):
        raise SnippetWriteError("ProxUI SSH key is not set up yet.")

    pkey = paramiko.Ed25519Key.from_private_key_file(_SSH_KEY_PATH)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.WarningPolicy())
    client.connect(
        hostname=host,
        port=22,
        username="root",
        pkey=pkey,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def _ssh_connect_root_password(node: str, password: str, timeout: int = 20):
    """Open a paramiko SSHClient to `node` as root using a password (for provisioning)."""
    paramiko = _get_paramiko()
    if not paramiko:
        raise SnippetWriteError("paramiko is not installed (pip install paramiko).")
    host, _ = _ssh_target_for_node(node)
    if not host:
        raise SnippetWriteError(f"No connection host known for node '{node}'.")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.WarningPolicy())
    try:
        client.connect(
            hostname=host,
            port=22,
            username="root",
            password=password,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
    except paramiko.AuthenticationException:
        raise SnippetWriteError("SSH login as root failed — check root's password.")
    return client


def _sftp_write_file(node: str, remote_path: str, content: str) -> None:
    """Write `content` to `remote_path` on `node` via SFTP as root, creating parent dirs."""
    client = _ssh_connect_root(node)
    try:
        sftp = client.open_sftp()
        # Ensure parent directories exist (mkdir -p semantics).
        parent = os.path.dirname(remote_path)
        parts = [p for p in parent.split("/") if p]
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                sftp.stat(cur)
            except IOError:
                try:
                    sftp.mkdir(cur)
                except IOError:
                    pass  # concurrent create or perms handled by the write below
        with sftp.open(remote_path, "w") as wf:
            wf.write(content)
    finally:
        client.close()


def _sftp_read_file(node: str, remote_path: str) -> str:
    """Read a file from `node` via SFTP as root. Raises on failure."""
    client = _ssh_connect_root(node)
    try:
        sftp = client.open_sftp()
        with sftp.open(remote_path, "r") as rf:
            return rf.read().decode("utf-8", errors="replace")
    finally:
        client.close()


def _ssh_run_root(
    node: str,
    command: str,
    timeout: int = 30,
    stdin_data: str | None = None,
    host: str | None = None,
) -> tuple[int, str, str]:
    """Run a command on `node` as root via SSH. Returns (exit_code, stdout, stderr).

    If stdin_data is given it is written to the command's stdin (e.g. to feed
    `chpasswd`) so secrets never appear on the command line. `host` overrides the
    address (needed for node-specific commands — see _ssh_connect_root).
    """
    client = _ssh_connect_root(node, timeout=timeout, host=host)
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        if stdin_data is not None:
            stdin.write(stdin_data)
            stdin.flush()
            try:
                stdin.channel.shutdown_write()
            except Exception:
                pass
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return rc, out, err
    finally:
        client.close()


def _ssh_port_open(node: str, timeout: float = 4.0, host: str | None = None) -> bool:
    """Quick TCP check for SSH (port 22) reachability. `host` overrides the address."""
    if host is None:
        host, _ = _ssh_target_for_node(node)
    if not host:
        return False
    try:
        with socket.create_connection((host, 22), timeout=timeout):
            return True
    except OSError:
        return False


# ───────────────────────────────────────────────────────────────────────────
# termproxy (node Shell over 8006) — Tier-3 fallback and key provisioning.
#
# The node Shell gives a root PTY over the API port, so it works even when SSH
# (port 22) is blocked. It requires the Sys.Console privilege and password auth
# (an API token cannot open a console session). We drive the PTY by base64ing
# the payload and echoing a unique sentinel with the exit code, so we can tell
# when a command finished and whether it succeeded despite the noisy terminal.
# ───────────────────────────────────────────────────────────────────────────


def _pve_login_ticket(node: str) -> tuple[str, str, str, bool]:
    """Return (host, PVEAuthCookie ticket, CSRF token, verify_ssl) for a node.

    Requires username/password auth; raises SnippetWriteError otherwise.
    """
    meta = connection_metadata.get(node) or {}
    host = meta.get("host")
    user = meta.get("user")
    password = meta.get("password")
    verify = meta.get("verify_ssl", True)
    if not host:
        raise SnippetWriteError(f"No connection host known for node '{node}'.")
    if not password:
        raise SnippetWriteError(
            "The node Shell (termproxy) needs username/password auth — an API "
            "token can't open a console session."
        )
    r = requests.post(
        f"https://{host}:8006/api2/json/access/ticket",
        data={"username": user, "password": password},
        verify=verify,
        timeout=15,
    )
    r.raise_for_status()
    d = r.json()["data"]
    return host, d["ticket"], d["CSRFPreventionToken"], verify


class _TermSession:
    """A driven root PTY over a Proxmox termproxy websocket."""

    def __init__(self, ws):
        self.ws = ws

    def _send_input(self, data: str) -> None:
        self.ws.send(f"0:{len(data.encode('utf-8'))}:{data}")

    def _drain_until(self, patterns: list, timeout: int = 8) -> tuple[int, str]:
        """Read until any regex in `patterns` matches. Returns (index, buffer); -1 if none."""
        compiled = [re.compile(p) for p in patterns]
        buf = ""
        deadline = time.time() + timeout
        try:
            self.ws.settimeout(min(timeout, 4))
        except Exception:
            pass
        while time.time() < deadline:
            try:
                c = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                for i, rx in enumerate(compiled):
                    if rx.search(buf):
                        return i, buf
                continue
            except Exception:
                break
            if isinstance(c, (bytes, bytearray)):
                c = c.decode("utf-8", errors="replace")
            buf += c
            for i, rx in enumerate(compiled):
                if rx.search(buf):
                    return i, buf
        return -1, buf

    def _login(self, os_user: str, os_password: str) -> None:
        """Drive an interactive `login:` prompt with OS credentials."""
        self._send_input(f"{os_user}\n")
        idx, _ = self._drain_until([r"[Pp]assword:\s*$"], timeout=8)
        if idx != 0:
            raise SnippetWriteError(
                "termproxy: no password prompt after sending username."
            )
        self._send_input(f"{os_password}\n")
        # Success → shell prompt; failure → 'Login incorrect' then another 'login:'.
        idx, buf = self._drain_until(
            [r"[#$]\s*$", r"[Ll]ogin incorrect", r"login:\s*$"], timeout=12
        )
        if idx != 0:
            raise SnippetWriteError(
                "termproxy: OS login failed (check the root password)."
            )

    def run(self, command: str, timeout: int = 45) -> tuple[int, str]:
        """Run a shell command, return (exit_code, captured_output)."""
        marker = f"__PROXUI_{uuid.uuid4().hex}__"
        # The echoed command contains `marker "$?"`; only the real output line
        # contains `marker:<digits>`, so the regex won't match the echo.
        self._send_input(f"{command}; printf '%s:%s\\n' {marker} \"$?\"\n")
        pat = re.compile(re.escape(marker) + r":(\d+)")
        buf = ""
        deadline = time.time() + timeout
        try:
            self.ws.settimeout(min(timeout, 10))
        except Exception:
            pass
        while time.time() < deadline:
            try:
                chunk = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            if isinstance(chunk, (bytes, bytearray)):
                chunk = chunk.decode("utf-8", errors="replace")
            if not chunk:
                continue
            buf += chunk
            m = pat.search(buf)
            if m:
                return int(m.group(1)), buf
        raise SnippetWriteError("termproxy: command timed out")

    def write_file(self, remote_path: str, content: str) -> None:
        """Write content to remote_path by streaming base64 through the PTY."""
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        tmp = f"/tmp/proxui-snip-{uuid.uuid4().hex}.b64"
        qdir = shlex.quote(os.path.dirname(remote_path) or "/")
        qfile = shlex.quote(remote_path)
        qtmp = shlex.quote(tmp)
        rc, out = self.run(f"mkdir -p {qdir} && : > {qtmp}")
        if rc != 0:
            raise SnippetWriteError(
                f"termproxy: could not prepare {qdir} ({out[-200:].strip()})"
            )
        # Append base64 in PTY-safe chunks (well under the ~4 KB line limit).
        for i in range(0, len(b64), 1024):
            chunk = b64[i : i + 1024]
            rc, out = self.run(f"printf '%s' {shlex.quote(chunk)} >> {qtmp}")
            if rc != 0:
                raise SnippetWriteError(
                    f"termproxy: chunk write failed ({out[-200:].strip()})"
                )
        rc, out = self.run(f"base64 -d {qtmp} > {qfile} && rm -f {qtmp}")
        if rc != 0:
            raise SnippetWriteError(
                f"termproxy: writing {remote_path} failed ({out[-200:].strip()})"
            )

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def _is_root_pam(node: str) -> bool:
    """True if the API connection for this node authenticates as root@pam."""
    meta = connection_metadata.get(node) or {}
    return (meta.get("user") or "") == "root@pam"


def _termproxy_open(
    node: str, os_password: str | None = None, timeout: int = 20
) -> _TermSession:
    """Open a root shell on `node` via termproxy over 8006.

    root@pam gets a passwordless root shell. Any other API user hits an
    interactive OS `login:` prompt, which we drive as root using `os_password`
    (root's OS password). Raises SnippetWriteError with an actionable message.
    """
    from urllib.parse import quote_plus

    host, login_ticket, csrf, verify = _pve_login_ticket(node)
    r = requests.post(
        f"https://{host}:8006/api2/json/nodes/{node}/termproxy",
        headers={
            "CSRFPreventionToken": csrf,
            "Cookie": f"PVEAuthCookie={login_ticket}",
        },
        verify=verify,
        timeout=timeout,
    )
    if r.status_code == 403:
        raise SnippetWriteError(
            "The node Shell was denied — the API user needs the 'Sys.Console' "
            "privilege on this node to open a console session."
        )
    r.raise_for_status()
    d = r.json()["data"]
    port, vncticket, tuser = d["port"], d["ticket"], d["user"]

    url = (
        f"wss://{host}:8006/api2/json/nodes/{node}/vncwebsocket"
        f"?port={port}&vncticket={quote_plus(vncticket)}"
    )
    sslopt = {} if verify else {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
    ws = websocket.create_connection(
        url,
        header=[f"Cookie: PVEAuthCookie={login_ticket}"],
        sslopt=sslopt,
        timeout=timeout,
    )
    ws.send(f"{tuser}:{vncticket}\n")
    first = ws.recv()
    if isinstance(first, (bytes, bytearray)):
        first = first.decode("utf-8", errors="replace")
    if not str(first).startswith("OK"):
        ws.close()
        raise SnippetWriteError(f"termproxy handshake rejected: {str(first)[:80]!r}")

    sess = _TermSession(ws)
    # Either a passwordless root shell (root@pam) or an interactive login prompt.
    idx, _buf = sess._drain_until([r"login:\s*$", r"[#$]\s*$"], timeout=8)
    if idx == 1:
        return sess  # already at a shell
    if idx == 0:
        pw = os_password
        if pw is None and _is_root_pam(node):
            pw = (connection_metadata.get(node) or {}).get("password")
        if not pw:
            sess.close()
            raise SnippetWriteError(
                "The node Shell opened an OS login prompt (your API user is not "
                "root@pam). Provide root's OS password to set up snippet access, "
                "or install ProxUI's key manually."
            )
        try:
            sess._login("root", pw)
        except SnippetWriteError:
            sess.close()
            raise
        return sess
    sess.close()
    raise SnippetWriteError(
        "termproxy: unexpected shell state (no login or shell prompt)."
    )


def _install_key_command(pub_line: str, replace: bool = False) -> str:
    """Idempotent shell to put ProxUI's pubkey in root's authorized_keys.

    replace=False (default): remove only *this* instance's own key (matched by
    its key material) before appending it, so re-running is idempotent while
    leaving other ProxUI instances' keys in place — multiple instances coexist.

    replace=True: remove every prior proxui-snippet@ key (all ProxUI instances)
    before adding this one, e.g. to clean up keys from decommissioned instances.
    """
    q = shlex.quote(pub_line)
    if replace:
        # Drop every ProxUI key regardless of which instance installed it.
        filter_cmd = "grep -v 'proxui-snippet@' /root/.ssh/authorized_keys"
    else:
        # Drop only our own key (by its unique key material), keep the rest.
        parts = pub_line.split()
        key_material = parts[1] if len(parts) >= 2 else pub_line
        filter_cmd = f"grep -vF {shlex.quote(key_material)} /root/.ssh/authorized_keys"
    return (
        "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
        "touch /root/.ssh/authorized_keys && "
        f"{filter_cmd} > /root/.ssh/.proxui_ak.tmp 2>/dev/null || true && "
        f"printf '%s\\n' {q} >> /root/.ssh/.proxui_ak.tmp && "
        "cat /root/.ssh/.proxui_ak.tmp > /root/.ssh/authorized_keys && "
        "rm -f /root/.ssh/.proxui_ak.tmp && chmod 600 /root/.ssh/authorized_keys && "
        "echo PROXUI_INSTALLED"
    )


def _provision_ssh_key(
    node: str, root_password: str | None = None, replace: bool = False
) -> dict:
    """Install ProxUI's public key into root's authorized_keys on `node`.

    Prefers SSH-as-root-with-password when port 22 is open; otherwise drives the
    node Shell (termproxy). Then verifies key-based SFTP works and detects whether
    root's authorized_keys is cluster-shared (pmxcfs). Returns a status dict.

    replace=True removes other ProxUI instances' keys too (see
    _install_key_command); the default preserves them.
    """
    _priv, pub = _ensure_ssh_key()
    install_cmd = _install_key_command(pub, replace=replace)

    if _ssh_port_open(node) and root_password:
        client = _ssh_connect_root_password(node, root_password)
        try:
            _in, out_s, err_s = client.exec_command(install_cmd, timeout=30)
            rc = out_s.channel.recv_exit_status()
            out = out_s.read().decode("utf-8", errors="replace")
            if rc != 0 or "PROXUI_INSTALLED" not in out:
                err = err_s.read().decode("utf-8", errors="replace")
                raise SnippetWriteError(
                    f"Key install failed: {(err or out)[-200:].strip()}"
                )
        finally:
            client.close()
        channel = "ssh-password"
    else:
        sess = _termproxy_open(node, os_password=root_password)
        try:
            rc, out = sess.run(install_cmd)
            if rc != 0 or "PROXUI_INSTALLED" not in out:
                raise SnippetWriteError(
                    f"Key install failed via node Shell: {out[-200:].strip()}"
                )
        finally:
            sess.close()
        channel = "termproxy"

    result = {
        "channel": channel,
        "sftp_ok": False,
        "shared_authorized_keys": False,
        "authorized_keys_path": "",
        "pubkey": pub,
    }
    # Verify key-based access works and inspect the authorized_keys target.
    if _ssh_port_open(node):
        try:
            rc, out, _err = _ssh_run_root(
                node, "readlink -f /root/.ssh/authorized_keys; echo PROXUI_SFTP_OK"
            )
            result["sftp_ok"] = "PROXUI_SFTP_OK" in out
            ak_path = next(
                (ln.strip() for ln in out.splitlines() if ln.startswith("/")), ""
            )
            result["authorized_keys_path"] = ak_path
            result["shared_authorized_keys"] = "/etc/pve/" in ak_path
        except Exception as e:
            result["verify_error"] = str(e)
    return result


# In-memory cache for snippet content (keyed by "node/vmid/type").
# Populated when ProxUI saves a snippet; used as fallback if filesystem
# access is unavailable.
_snippet_cache: dict = {}

# Persistent local store for snippet content. Proxmox has no REST endpoint to
# read snippet file contents back, and ProxUI usually runs on a different host
# than the nodes (so it can't read the storage path directly). We keep a copy
# of every snippet we write here so it can always be shown again after a
# restart, independent of node filesystem access or the in-memory cache.
_SNIPPET_STORE_DIR = os.path.join(os.path.dirname(CONFIG_FILE_PATH), "snippets")


def _snippet_store_path(node: str, storage: str, filename: str) -> str:
    safe = f"{node}__{storage}__{filename}".replace("/", "_").replace("..", "_")
    return os.path.join(_SNIPPET_STORE_DIR, safe)


def _store_snippet_local(node: str, storage: str, filename: str, content: str) -> None:
    try:
        os.makedirs(_SNIPPET_STORE_DIR, exist_ok=True)
        with open(_snippet_store_path(node, storage, filename), "w") as f:
            f.write(content)
    except OSError as exc:
        print(f"Warning: could not persist snippet locally: {exc}")


def _read_snippet_local(node: str, storage: str, filename: str) -> str | None:
    try:
        with open(_snippet_store_path(node, storage, filename)) as f:
            return f.read()
    except OSError:
        return None


_CLOUD_INIT_LIST_KEYS = frozenset(
    {
        "users",
        "packages",
        "runcmd",
        "bootcmd",
        "write_files",
        "ssh_authorized_keys",
        "groups",
        "fs_setup",
        "mounts",
        "cloud_config_modules",
        "cloud_final_modules",
        "cloud_init_modules",
    }
)
_CLOUD_INIT_BOOL_KEYS = frozenset(
    {
        "package_update",
        "package_upgrade",
        "package_reboot_if_required",
        "disable_root",
        "ssh_pwauth",
        "resize_rootfs",
        "manage_resolv_conf",
    }
)
_CLOUD_INIT_STR_KEYS = frozenset(
    {
        "hostname",
        "fqdn",
        "timezone",
        "locale",
        "final_message",
        "locale_configfile",
        "byobu_by_default",
        "merge_how",
    }
)
_CLOUD_INIT_DICT_KEYS = frozenset(
    {
        "apt",
        "yum_repos",
        "ntp",
        "power_state",
        "resolv_conf",
        "network",
        "ca_certs",
        "chpasswd",
        "puppet",
        "chef",
        "ansible",
        "datasource",
        "ssh_keys",
        "disk_setup",
        "snap",
        "keyboard",
        "growpart",
        "swap",
        "phone_home",
        "random_seed",
        "reporting",
        "output",
        "wireguard",
        "ubuntu_advantage",
        "landscape",
    }
)
_CLOUD_INIT_KNOWN = (
    _CLOUD_INIT_LIST_KEYS
    | _CLOUD_INIT_BOOL_KEYS
    | _CLOUD_INIT_STR_KEYS
    | _CLOUD_INIT_DICT_KEYS
    | {"manage_etc_hosts"}
)


def _validate_cloud_init_yaml(content: str) -> tuple[list, list]:
    """Return (errors, warnings) for cloud-init YAML content."""
    errors: list = []
    warnings: list = []

    content = content.strip()
    if not content:
        errors.append("Content is empty.")
        return errors, warnings

    lines = content.splitlines()
    first = lines[0].strip()
    if first != "#cloud-config":
        if first.startswith("#"):
            warnings.append(f"First line is '{first}', expected '#cloud-config'.")
        else:
            errors.append("First line must be '#cloud-config'.")

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        errors.append(f"YAML syntax error: {exc}")
        return errors, warnings

    if data is None:
        warnings.append("Config is effectively empty (only comment lines).")
        return errors, warnings

    if not isinstance(data, dict):
        errors.append(f"Top-level value must be a mapping, got {type(data).__name__}.")
        return errors, warnings

    for key, value in data.items():
        if key in _CLOUD_INIT_LIST_KEYS and not isinstance(value, list):
            errors.append(f"'{key}' must be a list, got {type(value).__name__}.")
        elif key in _CLOUD_INIT_BOOL_KEYS and not isinstance(value, bool):
            errors.append(f"'{key}' must be true/false, got {type(value).__name__}.")
        elif key in _CLOUD_INIT_STR_KEYS and not isinstance(value, str):
            errors.append(f"'{key}' must be a string, got {type(value).__name__}.")
        elif key in _CLOUD_INIT_DICT_KEYS and not isinstance(value, dict):
            errors.append(f"'{key}' must be a mapping, got {type(value).__name__}.")
        elif key not in _CLOUD_INIT_KNOWN:
            warnings.append(
                f"Unknown key '{key}' (may be valid for your cloud-init version)."
            )

    if "users" in data and isinstance(data["users"], list):
        for i, u in enumerate(data["users"]):
            if u == "default":
                continue
            if not isinstance(u, dict):
                errors.append(f"users[{i}] must be a mapping.")
            else:
                if "name" not in u:
                    errors.append(f"users[{i}] missing required 'name' field.")
                sk = u.get("ssh_authorized_keys")
                if sk is not None and not isinstance(sk, list):
                    errors.append(f"users[{i}].ssh_authorized_keys must be a list.")

    if "write_files" in data and isinstance(data["write_files"], list):
        for i, f in enumerate(data["write_files"]):
            if not isinstance(f, dict):
                errors.append(f"write_files[{i}] must be a mapping.")
            elif "path" not in f:
                errors.append(f"write_files[{i}] missing required 'path' field.")

    if "runcmd" in data and isinstance(data["runcmd"], list):
        for i, cmd in enumerate(data["runcmd"]):
            if not isinstance(cmd, (str, list)):
                errors.append(f"runcmd[{i}] must be a string or list of strings.")

    return errors, warnings


def _get_storage_path(node: str, storage: str) -> str | None:
    """Return the filesystem base path of a Proxmox storage, or None.

    Uses the configured `path` (dir storage) and falls back to the /mnt/pve/<name>
    convention for the network filesystem types that mount there.
    """
    try:
        proxmox = get_proxmox_connection(node, auto_renew=True)
        cfg = next(
            (s for s in proxmox.storage.get() if s.get("storage") == storage), {}
        )
        path = cfg.get("path", "")
        if path:
            return path
        if cfg.get("type") in ("nfs", "cifs", "cephfs", "glusterfs"):
            return f"/mnt/pve/{storage}"
        return None
    except Exception:
        return None


def _snippet_remote_path(node: str, storage: str, filename: str) -> str | None:
    """Return {storage_path}/snippets/{filename} for a node, or None if unknown."""
    base = _get_storage_path(node, storage)
    return os.path.join(base, "snippets", filename) if base else None


def _read_snippet(
    node: str, storage: str, filename: str, snippet_type: str = "user"
) -> str | None:
    """Read snippet content: node filesystem, SFTP, local store, then cache.

    SFTP is the authoritative remote source once ProxUI's key is installed; the
    local store is the fallback when the node is unreachable or after a restart
    (which clears the in-memory cache).
    """
    path = _get_storage_path(node, storage)
    if path:
        try:
            with open(os.path.join(path, "snippets", filename)) as f:
                return f.read()
        except OSError:
            pass
    # Remote read via SFTP (authoritative — reflects the file actually on the node).
    if os.path.exists(_SSH_KEY_PATH) and _ssh_port_open(node):
        remote = _snippet_remote_path(node, storage, filename)
        if remote:
            try:
                return _sftp_read_file(node, remote)
            except Exception:
                pass
    local = _read_snippet_local(node, storage, filename)
    if local is not None:
        return local
    return _snippet_cache.get(f"{node}/{storage}/{snippet_type}")


def _write_snippet(
    node: str, storage: str, filename: str, content: str, snippet_type: str = "user"
) -> str:
    """Write a snippet file to the storage's snippets/ directory. Returns the tier used.

    Proxmox has no REST API to upload snippets, so we place the file directly, in
    order of preference:
      1. local filesystem  — ProxUI runs on the node / storage is mounted
      2. SFTP as root       — remote, port 22 open, ProxUI key installed
      3. node Shell (root@pam) — remote, port 22 blocked, passwordless console
    The content is always kept in the local store first so nothing is lost, and a
    clear SnippetWriteError explains what to do if no channel works.
    """
    # Keep a copy no matter what happens next.
    _snippet_cache[f"{node}/{storage}/{snippet_type}"] = content
    _store_snippet_local(node, storage, filename, content)

    path = _get_storage_path(node, storage)
    remote_path = os.path.join(path, "snippets", filename) if path else None

    # Tier 1: local filesystem.
    if path:
        try:
            snippets_dir = os.path.join(path, "snippets")
            os.makedirs(snippets_dir, exist_ok=True)
            with open(os.path.join(snippets_dir, filename), "w") as f:
                f.write(content)
            return "local"
        except OSError:
            pass

    if not remote_path:
        raise SnippetWriteError(
            f"Storage '{storage}' on node '{node}' has no resolvable filesystem "
            f"path, so the snippet can't be written. Pick a directory/NFS/CephFS "
            f"storage. Your content is saved in ProxUI."
        )

    errors = []

    # Tier 2: SFTP as root with ProxUI's key.
    if os.path.exists(_SSH_KEY_PATH) and _ssh_port_open(node):
        try:
            _sftp_write_file(node, remote_path, content)
            return "sftp"
        except Exception as e:
            errors.append(f"SFTP: {e}")

    # Tier 3: node Shell (only usable without a prompt for root@pam here, since a
    # normal save has no OS password to offer).
    if _is_root_pam(node):
        try:
            sess = _termproxy_open(node)
            try:
                sess.write_file(remote_path, content)
            finally:
                sess.close()
            return "termproxy"
        except Exception as e:
            errors.append(f"node Shell: {e}")

    hint = (
        "Set up snippet access (install ProxUI's SSH key) from the Cloud-Init tab, "
        "or run ProxUI where it can reach the storage. Your content is saved in ProxUI."
    )
    detail = (" [" + "; ".join(errors) + "]") if errors else ""
    raise SnippetWriteError(
        f"Couldn't write the snippet to {remote_path} on node '{node}'.{detail} {hint}"
    )


def _parse_cicustom(cicustom: str) -> dict:
    """Parse 'user=local:snippets/f.yaml,network=...' into a dict."""
    result: dict = {}
    for part in (cicustom or "").split(","):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            result[k.strip()] = v.strip()
    return result


def _build_cicustom(parts: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in parts.items() if v)


# ---------------------------------------------------------------------------
# Cloud-init API routes
# ---------------------------------------------------------------------------


@app.route("/api/snippets/ssh/status")
def api_snippets_ssh_status():
    """Report ProxUI's SSH-key state for snippet writes."""
    pub = _ssh_public_key()
    node = request.args.get("node")
    resp = {
        "paramiko_available": _get_paramiko() is not None,
        "key_exists": pub is not None,
        "pubkey": pub or "",
        "comment": (pub.split()[-1] if pub and len(pub.split()) >= 3 else ""),
    }
    if node:
        resp["ssh_port_open"] = _ssh_port_open(node)
        resp["root_pam"] = _is_root_pam(node)
        # Does key-based SFTP already work?
        resp["sftp_ok"] = False
        if pub and resp["ssh_port_open"]:
            try:
                rc, out, _ = _ssh_run_root(node, "echo PROXUI_SFTP_OK")
                resp["sftp_ok"] = "PROXUI_SFTP_OK" in out
            except Exception:
                resp["sftp_ok"] = False
    return jsonify(resp)


@app.route("/api/snippets/ssh/setup", methods=["POST"])
def api_snippets_ssh_setup():
    """Generate (if needed) and install ProxUI's public key into root's authorized_keys.

    Body: { node, root_password?, replace? }. root_password is required unless
    the API user is root@pam (passwordless node Shell). replace=true also removes
    other ProxUI instances' keys; the default keeps them.
    """
    data = request.get_json() or {}
    node = data.get("node")
    root_password = data.get("root_password") or None
    replace = bool(data.get("replace"))
    if not node:
        return jsonify({"error": "node is required"}), 400
    if not get_proxmox_connection(node, auto_renew=True):
        return jsonify({"error": "Node not found"}), 404
    try:
        result = _provision_ssh_key(node, root_password=root_password, replace=replace)
        result["success"] = True
        result["message"] = (
            "SSH key installed. Snippet writes will use SFTP as root."
            if result.get("sftp_ok")
            else "SSH key installed (verification limited — port 22 may be closed)."
        )
        return jsonify(result)
    except SnippetWriteError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return _proxmox_error_response(e)


def _any_proxmox():
    """Return any working Proxmox connection, or None."""
    if not proxmox_nodes:
        return None
    return get_proxmox_connection(next(iter(proxmox_nodes)), auto_renew=True)


@app.route("/api/snippets/storages")
def api_snippets_storages():
    """List cluster storages for snippet placement, flagged shared / snippets-enabled.

    Returns filesystem-type storages (only those can hold snippets), so the UI can
    prefer shared ones and offer to enable snippets on a chosen storage.
    """
    node = request.args.get("node")
    proxmox = get_proxmox_connection(node, auto_renew=True) if node else _any_proxmox()
    if not proxmox:
        return jsonify({"error": "No Proxmox connection"}), 404
    try:
        out = []
        for s in proxmox.storage.get():
            stype = s.get("type", "")
            content = [c for c in (s.get("content") or "").split(",") if c]
            out.append(
                {
                    "storage": s.get("storage"),
                    "type": stype,
                    "shared": bool(s.get("shared", 0)),
                    "snippets_enabled": "snippets" in content,
                    "filesystem": stype
                    in ("dir", "nfs", "cifs", "cephfs", "glusterfs"),
                    "path": s.get("path", ""),
                }
            )
        return jsonify(out)
    except Exception as e:
        return _proxmox_error_response(e)


@app.route("/api/snippets/storage/create", methods=["POST"])
def api_snippets_storage_create():
    """Create a cluster-shared snippet storage on pmxcfs (/etc/pve/<name>).

    Files written there replicate to all nodes, so snippets survive VM migration.
    Uses the Proxmox API, falling back to `pvesm` over root SSH.
    """
    data = request.get_json() or {}
    node = data.get("node")
    name = (data.get("name") or "proxui-snippets").strip()
    path = data.get("path") or f"/etc/pve/{name}"
    proxmox = get_proxmox_connection(node, auto_renew=True) if node else _any_proxmox()
    if not proxmox:
        return jsonify({"error": "No Proxmox connection"}), 404
    try:
        existing = {s.get("storage") for s in proxmox.storage.get()}
        if name in existing:
            return jsonify(
                {
                    "success": True,
                    "message": f"Storage '{name}' already exists.",
                    "storage": name,
                }
            )
        try:
            proxmox.storage.post(
                storage=name,
                type="dir",
                path=path,
                content="snippets",
                shared=1,
                mkdir=1,
            )
        except Exception as api_err:
            # Fall back to root SSH (e.g. API user lacks Datastore.Allocate).
            if not node:
                raise api_err
            cmd = (
                f"mkdir -p {shlex.quote(path)} && "
                f"pvesm add dir {shlex.quote(name)} --path {shlex.quote(path)} "
                f"--content snippets --shared 1"
            )
            rc, out, err = _ssh_run_root(node, cmd)
            if rc != 0:
                return (
                    jsonify(
                        {
                            "error": f"Could not create storage: {(err or out)[-200:].strip()}"
                        }
                    ),
                    400,
                )
        return jsonify(
            {
                "success": True,
                "message": f"Created shared snippet storage '{name}' at {path} (replicated across the cluster).",
                "storage": name,
            }
        )
    except Exception as e:
        return _proxmox_error_response(e)


@app.route("/api/snippets/storage/enable-snippets", methods=["POST"])
def api_snippets_storage_enable():
    """Add the 'snippets' content type to an existing storage (API, or pvesm via SSH)."""
    data = request.get_json() or {}
    node = data.get("node")
    storage = data.get("storage")
    if not storage:
        return jsonify({"error": "storage is required"}), 400
    proxmox = get_proxmox_connection(node, auto_renew=True) if node else _any_proxmox()
    if not proxmox:
        return jsonify({"error": "No Proxmox connection"}), 404
    try:
        cur = next(
            (s for s in proxmox.storage.get() if s.get("storage") == storage), None
        )
        if not cur:
            return jsonify({"error": f"Storage '{storage}' not found"}), 404
        content = [c for c in (cur.get("content") or "").split(",") if c]
        if "snippets" in content:
            return jsonify(
                {
                    "success": True,
                    "message": "snippets already enabled.",
                    "storage": storage,
                }
            )
        content.append("snippets")
        joined = ",".join(content)
        try:
            proxmox.storage(storage).put(content=joined)
        except Exception as api_err:
            if not node:
                raise api_err
            rc, out, err = _ssh_run_root(
                node,
                f"pvesm set {shlex.quote(storage)} --content {shlex.quote(joined)}",
            )
            if rc != 0:
                return (
                    jsonify(
                        {
                            "error": f"Could not enable snippets: {(err or out)[-200:].strip()}"
                        }
                    ),
                    400,
                )
        return jsonify(
            {
                "success": True,
                "message": f"Enabled snippets on '{storage}'.",
                "storage": storage,
            }
        )
    except Exception as e:
        return _proxmox_error_response(e)


@app.route("/api/node/<node>/storages/snippets")
def api_node_snippet_storages(node):
    """List storages on this node that have the snippets content type enabled."""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404
    try:
        storages = proxmox.nodes(node).storage.get()
        result = [
            {
                "storage": s["storage"],
                "type": s.get("type", ""),
                "active": s.get("active", 1),
            }
            for s in storages
            if "snippets" in s.get("content", "").split(",")
            and s.get("enabled", 1) != 0
        ]
        return jsonify(result)
    except Exception as e:
        return _proxmox_error_response(e)


@app.route("/api/vm/<node>/<vmid>/cloudinit")
def api_vm_cloudinit_get(node, vmid):
    """Return all cloud-init related config for a QEMU VM."""
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404
    try:
        config = proxmox.nodes(node).qemu(vmid).config.get()
        sshkeys_raw = config.get("sshkeys", "")
        cicustom = config.get("cicustom", "")
        ci_parts = _parse_cicustom(cicustom)

        # Try to read user snippet content if attached
        snippet_content = None
        snippet_storage = ""
        snippet_filename = ""
        user_val = ci_parts.get("user", "")
        if user_val and ":" in user_val:
            snippet_storage, _, snippet_path = user_val.partition(":")
            snippet_filename = snippet_path.split("/")[-1]
            snippet_content = _read_snippet(
                node, snippet_storage, snippet_filename, "user"
            )

        return jsonify(
            {
                "ciuser": config.get("ciuser", ""),
                "sshkeys": unquote(sshkeys_raw) if sshkeys_raw else "",
                "nameserver": config.get("nameserver", ""),
                "searchdomain": config.get("searchdomain", ""),
                "ipconfig0": config.get("ipconfig0", ""),
                "cicustom": cicustom,
                "ci_parts": ci_parts,
                "snippet_content": snippet_content,
                "snippet_storage": snippet_storage,
                "snippet_filename": snippet_filename,
            }
        )
    except Exception as e:
        return _proxmox_error_response(e)


def _regenerate_cloudinit_drive(proxmox, node, vmid) -> bool:
    """Best-effort regenerate of the cloud-init drive (qm cloudinit update).

    Returns True on success. Non-fatal: a VM may have no cloud-init disk, or the
    node may be momentarily busy — callers surface the flag rather than failing
    the whole operation, since the config change itself already succeeded.
    """
    try:
        proxmox.nodes(node).qemu(vmid).cloudinit.put()
        return True
    except Exception as e:
        app.logger.warning("cloud-init drive regen failed for %s/%s: %s", node, vmid, e)
        return False


@app.route("/api/vm/<node>/<vmid>/cloudinit/regenerate", methods=["POST"])
def api_vm_cloudinit_regenerate(node, vmid):
    """Regenerate the VM's cloud-init drive from its current config (qm cloudinit update).

    Refreshes the small cidata disk without a stop/start. Note: an
    already-provisioned guest still won't re-run per-instance modules until
    'cloud-init clean' + reboot inside the guest.
    """
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404
    try:
        proxmox.nodes(node).qemu(vmid).cloudinit.put()
        return jsonify(
            {
                "success": True,
                "message": "Cloud-init drive regenerated. Reboot the VM to apply; an "
                "already-provisioned guest may also need 'cloud-init clean' first.",
            }
        )
    except Exception as e:
        return _proxmox_error_response(e)


@app.route("/api/vm/<node>/<vmid>/cloudinit/native", methods=["PUT"])
def api_vm_cloudinit_native(node, vmid):
    """Update native Proxmox cloud-init fields (ciuser, sshkeys, nameserver, etc.)."""
    data = request.get_json() or {}
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404
    try:
        params: dict = {}
        if "ciuser" in data:
            params["ciuser"] = data["ciuser"] or ""
        if "sshkeys" in data:
            raw = (data["sshkeys"] or "").strip()
            params["sshkeys"] = quote(raw, safe="") if raw else ""
        if "nameserver" in data:
            params["nameserver"] = data["nameserver"] or ""
        if "searchdomain" in data:
            params["searchdomain"] = data["searchdomain"] or ""
        if "ipconfig0" in data:
            params["ipconfig0"] = data["ipconfig0"] or ""
        if not params:
            return jsonify({"error": "No fields to update"}), 400
        proxmox.nodes(node).qemu(vmid).config.put(**params)
        return jsonify({"success": True, "message": "Cloud-init settings saved."})
    except Exception as e:
        return _proxmox_error_response(e)


@app.route("/api/vm/<node>/<vmid>/cloudinit/validate", methods=["POST"])
def api_vm_cloudinit_validate(node, vmid):
    """Validate cloud-init YAML content."""
    data = request.get_json() or {}
    content = data.get("content", "")
    if not content:
        return jsonify(
            {"valid": False, "errors": ["Content is empty."], "warnings": []}
        )
    errors, warnings = _validate_cloud_init_yaml(content)
    return jsonify({"valid": len(errors) == 0, "errors": errors, "warnings": warnings})


@app.route("/api/vm/<node>/<vmid>/cloudinit/snippet", methods=["POST"])
def api_vm_cloudinit_snippet_save(node, vmid):
    """Save a cloud-init snippet to storage and attach it to the VM via cicustom."""
    data = request.get_json() or {}
    storage = data.get("storage", "")
    filename = (data.get("filename") or f"{vmid}-user-data.yaml").strip()
    content = data.get("content", "")
    snippet_type = data.get("type", "user")

    if not storage:
        return jsonify({"error": "storage is required"}), 400
    if not content:
        return jsonify({"error": "content is required"}), 400
    if not filename.endswith((".yaml", ".yml", ".cfg")):
        filename += ".yaml"

    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        tier = _write_snippet(node, storage, filename, content, snippet_type)
    except SnippetWriteError as e:
        # File couldn't be placed on the node — don't attach a dangling cicustom.
        return jsonify({"error": str(e)}), 400

    try:
        volid = f"{storage}:snippets/{filename}"

        # Merge into existing cicustom
        config = proxmox.nodes(node).qemu(vmid).config.get()
        ci_parts = _parse_cicustom(config.get("cicustom", ""))
        ci_parts[snippet_type] = volid
        proxmox.nodes(node).qemu(vmid).config.put(cicustom=_build_cicustom(ci_parts))

        # Regenerate the cloud-init drive so the new snippet is on the disk the
        # guest reads at next boot. Changing cicustom alone leaves a running VM
        # serving its old (stale) drive until it's regenerated or cold-booted.
        # Proxmox derives instance-id from the user-data, so a content change
        # bumps the instance-id and cloud-init re-runs per-instance modules.
        regenerated = _regenerate_cloudinit_drive(proxmox, node, vmid)

        via = {
            "local": "local filesystem",
            "sftp": "SFTP",
            "termproxy": "node Shell",
        }.get(tier, tier)
        if regenerated:
            msg = (
                f"Snippet saved (via {via}) and attached as {snippet_type} ({volid}). "
                "Cloud-init drive regenerated — reboot the VM to apply."
            )
        else:
            msg = (
                f"Snippet saved (via {via}) and attached as {snippet_type} ({volid}). "
                'Could not regenerate the drive automatically — use "Regenerate drive", '
                "then reboot the VM to apply."
            )
        return jsonify(
            {
                "success": True,
                "message": msg,
                "volid": volid,
                "cicustom": _build_cicustom(ci_parts),
                "tier": tier,
                "regenerated": regenerated,
            }
        )
    except Exception as e:
        return _proxmox_error_response(e)


@app.route("/api/vm/<node>/<vmid>/cloudinit/snippet", methods=["DELETE"])
def api_vm_cloudinit_snippet_detach(node, vmid):
    """Detach a cloud-init snippet type from the VM's cicustom config."""
    data = request.get_json() or {}
    snippet_type = data.get("type", "user")

    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404
    try:
        config = proxmox.nodes(node).qemu(vmid).config.get()
        ci_parts = _parse_cicustom(config.get("cicustom", ""))
        if snippet_type not in ci_parts:
            return jsonify({"error": f"No {snippet_type} snippet attached"}), 400
        del ci_parts[snippet_type]
        new_cicustom = _build_cicustom(ci_parts)
        if new_cicustom:
            proxmox.nodes(node).qemu(vmid).config.put(cicustom=new_cicustom)
        else:
            proxmox.nodes(node).qemu(vmid).config.put(delete="cicustom")
        _snippet_cache.pop(f"{node}/{data.get('storage', '')}/user", None)
        # Regenerate so the drive reflects the detach (back to native ci* config
        # when nothing custom remains) on next reboot.
        regenerated = _regenerate_cloudinit_drive(proxmox, node, vmid)
        msg = f"{snippet_type} snippet detached."
        if regenerated:
            msg += " Cloud-init drive regenerated — reboot the VM to apply."
        return jsonify({"success": True, "message": msg, "regenerated": regenerated})
    except Exception as e:
        return _proxmox_error_response(e)


@app.route("/api/vm/<node>/<vmid>/agent", methods=["PUT"])
def api_vm_agent(node, vmid):
    """Enable or disable the QEMU guest agent, preserving any existing agent options."""
    data = request.get_json()
    if data is None or "enabled" not in data:
        return jsonify({"error": "enabled field is required"}), 400

    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        config = proxmox.nodes(node).qemu(vmid).config.get()
        current = str(config.get("agent", "0"))
        # Preserve any key=value options that follow the enabled flag
        parts = current.split(",")
        extra = [p for p in parts[1:] if p.strip()]
        flag = "1" if data["enabled"] else "0"
        agent_val = ",".join([flag] + extra) if extra else flag
        proxmox.nodes(node).qemu(vmid).config.put(agent=agent_val)
        state = "enabled" if data["enabled"] else "disabled"
        return jsonify(
            {
                "success": True,
                "message": f"Guest agent {state}. Takes effect on next VM start.",
            }
        )
    except Exception as e:
        return _proxmox_error_response(e)


def _node_address(node: str) -> str | None:
    """Real management address of a specific cluster node, for node-local commands.

    connection_metadata maps discovered nodes to the configured API endpoint, so
    it can't reach a *specific* node (e.g. to run `pct exec`, which must run on
    the node that owns the container). Look the node's IP up via cluster status.
    """
    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return None
    try:
        for entry in proxmox.cluster.status.get():
            if (
                entry.get("type") == "node"
                and entry.get("name") == node
                and entry.get("ip")
            ):
                return entry["ip"]
    except Exception:
        return None
    return None


def _reset_lxc_root_password(node: str, vmid: str, password: str) -> str:
    """Set an LXC container's root password by running chpasswd via `pct exec`.

    Proxmox has no REST API to exec inside a container (unlike QEMU's guest
    agent), so we run `pct exec <vmid> -- chpasswd` on the node over the same
    channels the snippet writer uses, feeding `root:<password>` on stdin so the
    secret never lands in the process list or shell history:
      1. SSH as root  — ProxUI's key installed and port 22 open (stdin pipe)
      2. node Shell    — root@pam console, via a short-lived temp file
    Returns the channel used, or raises SnippetWriteError with what to fix.
    """
    payload = f"root:{password}\n"
    qvmid = shlex.quote(str(vmid))
    errors = []

    # `pct exec` must run ON the node that owns the container. connection_metadata
    # points discovered nodes at the configured API endpoint, so resolve the
    # owning node's real address for the SSH tier. (The termproxy tier already
    # reaches the right node — /nodes/<node>/termproxy is proxied there.)
    host = _node_address(node)

    # Tier 1: SSH as root with ProxUI's key — password stays on the stdin pipe.
    if os.path.exists(_SSH_KEY_PATH) and host and _ssh_port_open(node, host=host):
        try:
            rc, out, err = _ssh_run_root(
                node,
                f"pct exec {qvmid} -- chpasswd",
                stdin_data=payload,
                host=host,
            )
            if rc == 0:
                return "ssh"
            errors.append(f"SSH: {(err or out)[-200:].strip()}")
        except Exception as e:
            errors.append(f"SSH: {e}")

    # Tier 2: node Shell (root@pam) — write the payload to a temp file, feed it to
    # chpasswd, then remove it.
    if _is_root_pam(node):
        try:
            sess = _termproxy_open(node)
            try:
                tmp = f"/tmp/proxui-pw-{uuid.uuid4().hex}"
                qtmp = shlex.quote(tmp)
                sess.write_file(tmp, payload)
                rc, out = sess.run(
                    f"pct exec {qvmid} -- chpasswd < {qtmp}; "
                    f"rc=$?; rm -f {qtmp}; exit $rc"
                )
                if rc == 0:
                    return "termproxy"
                errors.append(f"node Shell: {out[-200:].strip()}")
            finally:
                sess.close()
        except Exception as e:
            errors.append(f"node Shell: {e}")

    detail = (
        "; ".join(errors)
        if errors
        else (
            "no usable channel — set up ProxUI SSH access to the node "
            "(Cloud-Init → Custom Snippet → Set up access), or connect as root@pam."
        )
    )
    raise SnippetWriteError(f"Could not reset the container password: {detail}")


@app.route("/api/vm/<node>/<vmid>/reset-password", methods=["POST"])
def api_vm_reset_password(node, vmid):
    """Reset root password: LXC via pct exec, QEMU via guest agent with cipassword fallback."""
    data = request.get_json()
    if not data or not data.get("password"):
        return jsonify({"error": "Password is required"}), 400

    password = data["password"]
    vm_type = data.get("vm_type", "qemu")

    proxmox = get_proxmox_connection(node, auto_renew=True)
    if not proxmox:
        return jsonify({"error": "Node not found"}), 404

    try:
        if vm_type == "lxc":
            status = proxmox.nodes(node).lxc(vmid).status.current.get()
            if status.get("status") != "running":
                return (
                    jsonify({"error": "Container must be running to reset password"}),
                    400,
                )
            try:
                channel = _reset_lxc_root_password(node, vmid, password)
            except SnippetWriteError as e:
                return jsonify({"error": str(e)}), 400
            return jsonify(
                {
                    "success": True,
                    "message": f"Root password updated successfully (via {channel}).",
                }
            )
        else:
            # Try guest agent first (immediate effect), fall back to cipassword (next reboot)
            status = proxmox.nodes(node).qemu(vmid).status.current.get()
            is_running = status.get("status") == "running"
            if is_running:
                try:
                    proxmox.nodes(node).qemu(vmid).agent.exec.post(
                        command=["chpasswd"],
                        **{"input-data": f"root:{password}\n"},
                    )
                    return jsonify(
                        {
                            "success": True,
                            "message": "Root password updated successfully via guest agent",
                        }
                    )
                except Exception:
                    pass  # agent not available — fall through to cipassword

            proxmox.nodes(node).qemu(vmid).config.put(cipassword=password)
            return jsonify(
                {
                    "success": True,
                    "message": "Password set via cloud-init (cipassword). It will apply on next reboot or cloud-init run.",
                    "reboot_required": True,
                }
            )
    except Exception as e:
        return _proxmox_error_response(e)


@app.errorhandler(500)
def internal_error(error):
    return render_template("500.html"), 500


if __name__ == "__main__":
    print("Initializing cluster configurations...")
    init_all_clusters()

    print("Initializing Proxmox connections...")
    success = init_proxmox_connections()

    if not success or not proxmox_nodes:
        print("WARNING: No Proxmox connections established!")
    else:
        print(f"Successfully connected to {len(proxmox_nodes)} Proxmox nodes")
        print(
            f"Current cluster '{current_cluster_id}' contains {len(cluster_nodes)} total nodes"
        )

    debug_enabled = os.environ.get("PROXUI_DEBUG", "").lower() in (
        "1",
        "true",
        "yes",
    )
    host = os.environ.get("PROXUI_HOST", "0.0.0.0")
    port = int(os.environ.get("PROXUI_PORT", "8080"))
    app.run(debug=debug_enabled, host=host, port=port)
