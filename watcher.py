# watcher.py
import os
import time
import logging
import threading
import requests
from kubernetes import client, config, watch
from base64 import b64encode

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# Atlas config from env vars
CLIENT_ID     = os.environ["ATLAS_CLIENT_ID"]
CLIENT_SECRET = os.environ["ATLAS_CLIENT_SECRET"]
PROJECT_ID    = os.environ["ATLAS_PROJECT_ID"]
COMMENT_TAG   = os.environ.get("ATLAS_COMMENT_TAG", "k8s-node")

ATLAS_BASE  = f"https://cloud.mongodb.com/api/atlas/v2/groups/{PROJECT_ID}/accessList"
TOKEN_URL   = "https://cloud.mongodb.com/api/oauth/token"
API_VERSION = "application/vnd.atlas.2025-03-12+json"

# --- Token management ---

_token = None
_token_expiry = 0

def get_token():
    global _token, _token_expiry
    if time.time() < _token_expiry - 60:  # refresh 1 min before expiry
        return _token
    
    credentials = b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode().strip()
    r = requests.post(TOKEN_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json"
        },
        data="grant_type=client_credentials"
    )
    r.raise_for_status()
    data = r.json()
    _token = data["access_token"]
    _token_expiry = time.time() + data["expires_in"]
    return _token

def auth_headers():
    return {
        "Authorization": f"Bearer {get_token()}",
        "Accept": API_VERSION,
        "Content-Type": "application/json"
    }

# --- Atlas API helpers ---

def atlas_list_k8s_tagged():
    """Returns set of IP addresses in Atlas allowlist that are tagged as k8s nodes"""
    r = requests.get(ATLAS_BASE, headers=auth_headers())
    r.raise_for_status()
    k8s_ips = set()
    for entry in r.json().get("results", []):
        comment = entry.get("comment", "")
        if comment.startswith(COMMENT_TAG + "/"):
            k8s_ips.add(entry["ipAddress"])
    return k8s_ips

def atlas_add(ip, node_name):
    entry = f"{ip}/32"
    r = requests.post(ATLAS_BASE, headers=auth_headers(),
        json=[{"ipAddress": entry, "comment": f"{COMMENT_TAG}/{node_name}"}])
    if r.status_code == 409:
        log.info(f"IP {entry} already in Atlas allowlist, skipping")
        return
    r.raise_for_status()
    log.info(f"Added {entry} ({node_name}) to Atlas allowlist")

def atlas_remove(ip):
    entry = requests.utils.quote(f"{ip}/32", safe="")
    r = requests.delete(f"{ATLAS_BASE}/{entry}", headers=auth_headers())
    if r.status_code == 404:
        log.info(f"IP {entry} not in Atlas allowlist, skipping")
        return
    r.raise_for_status()
    log.info(f"Removed {entry} from Atlas allowlist")


# --- K8s helpers ---
def get_node_ip(node):
    for addr in (node.status.addresses or []):
        if addr.type == "ExternalIP":
            return addr.address
    return None

def list_all_node_ips():
    v1 = client.CoreV1Api()
    nodes = v1.list_node()
    return {get_node_ip(n): n.metadata.name 
        for n in nodes.items if get_node_ip(n)}

def reconcile():
    while True:
        try:
            k8s_ip_map = list_all_node_ips()  # {ip: node_name}
            k8s_ips = set(k8s_ip_map.keys())
            atlas_k8s_ips = atlas_list_k8s_tagged()  # only tagged IPs

            for ip in k8s_ips - atlas_k8s_ips:
                log.warning(f"Reconcile: {ip} missing from Atlas, adding")
                atlas_add(ip, k8s_ip_map[ip])

            for ip in atlas_k8s_ips - k8s_ips:
                log.warning(f"Reconcile: {ip} stale in Atlas, removing")
                atlas_remove(ip)

            log.info("Reconcile complete")
        except Exception as e:
            log.error(f"Reconcile error: {e}")

        time.sleep(300)

# --- Event watcher ---

def watch_nodes():
    v1 = client.CoreV1Api()
    w = watch.Watch()
    while True:
        try:
            for event in w.stream(v1.list_node, timeout_seconds=3600):
                event_type = event["type"]
                node       = event["object"]
                node_name  = node.metadata.name
                ip         = get_node_ip(node)

                if not ip:
                    continue

                if event_type == "ADDED":
                    atlas_add(ip, node_name)
                elif event_type == "DELETED":
                    atlas_remove(ip)
                # MODIFIED: ignored unless you want to track IP changes
        except Exception as e:
            log.error(f"Watch error: {e}, restarting in 5s")
            time.sleep(5)


# --- Entrypoint ---

if __name__ == "__main__":
    # Load k8s config (in-cluster when running as a pod)
    try:
        config.load_incluster_config()
    except:
        config.load_kube_config()  # fallback for local dev

    log.info("Starting Atlas node IP watcher")

    # Run reconciler in background thread
    t = threading.Thread(target=reconcile, daemon=True)
    t.start()

    # Run event watcher in main thread
    watch_nodes()