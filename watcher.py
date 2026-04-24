# watcher.py
import os
import time
import logging
import threading
import requests
from requests.auth import HTTPDigestAuth
from kubernetes import client, config, watch

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# Atlas config from env vars
PROJECT_ID  = os.environ["ATLAS_PROJECT_ID"]
PUBLIC_KEY  = os.environ["ATLAS_PUBLIC_KEY"]
PRIVATE_KEY = os.environ["ATLAS_PRIVATE_KEY"]
COMMENT_TAG = os.environ.get("ATLAS_COMMENT_TAG", "k8s-node")

ATLAS_BASE = f"https://cloud.mongodb.com/api/atlas/v1.0/groups/{PROJECT_ID}/accessList"
AUTH = HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY)


# --- Atlas API helpers ---

def atlas_list():
    """Returns set of all IP addresses in Atlas allowlist"""
    r = requests.get(ATLAS_BASE, auth=AUTH)
    r.raise_for_status()
    return {e["ipAddress"] for e in r.json().get("results", [])}

def atlas_list_k8s_tagged():
    """Returns set of IP addresses in Atlas allowlist that are tagged as k8s nodes"""
    r = requests.get(ATLAS_BASE, auth=AUTH)
    r.raise_for_status()
    k8s_ips = set()
    for entry in r.json().get("results", []):
        comment = entry.get("comment", "")
        if comment.startswith(COMMENT_TAG + "/"):
            k8s_ips.add(entry["ipAddress"])
    return k8s_ips

def atlas_add(ip, node_name):
    entry = f"{ip}/32"
    r = requests.post(ATLAS_BASE, auth=AUTH,
        json=[{"ipAddress": entry, "comment": f"{COMMENT_TAG}/{node_name}"}])
    if r.status_code == 409:
        log.info(f"IP {entry} already in Atlas allowlist, skipping")
        return
    r.raise_for_status()
    log.info(f"Added {entry} ({node_name}) to Atlas allowlist")

def atlas_remove(ip):
    entry = f"{ip}/32"
    r = requests.delete(f"{ATLAS_BASE}/{entry}", auth=AUTH)
    if r.status_code == 404:
        log.info(f"IP {entry} not in Atlas allowlist, skipping")
        return
    r.raise_for_status()
    log.info(f"Removed {entry} from Atlas allowlist")


# --- K8s helpers ---

def get_node_ip(node):
    for addr in (node.status.addresses or []):
        if addr.type == "InternalIP":
            return addr.address
    return None

def list_all_node_ips():
    v1 = client.CoreV1Api()
    nodes = v1.list_node()
    return {get_node_ip(n) for n in nodes.items if get_node_ip(n)}


# --- Reconciliation loop ---

def reconcile():
    while True:
        try:
            k8s_ips = list_all_node_ips()
            atlas_all_ips = atlas_list()
            atlas_k8s_ips = atlas_list_k8s_tagged()

            # Add missing k8s IPs to Atlas
            for ip in k8s_ips - atlas_all_ips:
                log.warning(f"Reconcile: {ip} missing from Atlas, adding")
                atlas_add(ip, "reconciled")

            # Remove stale k8s-tagged IPs from Atlas (only those tagged as k8s nodes)
            for ip in atlas_k8s_ips - k8s_ips:
                log.warning(f"Reconcile: {ip} stale k8s-tagged IP in Atlas, removing")
                atlas_remove(ip)

            log.info("Reconcile complete")
        except Exception as e:
            log.error(f"Reconcile error: {e}")

        time.sleep(300)  # every 5 minutes


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