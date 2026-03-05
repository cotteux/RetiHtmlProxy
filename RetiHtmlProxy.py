#!/usr/bin/env python3
"""
RetiHtmlProxy.py – HTTP → Reticulum proxy (GET / HEAD only)

Usage:
    python RetiHtmlProxy.py  [--port 8080]  [--verbose]

The proxy listens on the given TCP port, forwards each request over
Reticulum, and streams the reply back to the browser.

The code is a thin wrapper around the MeshCurl logic you already
have.  It keeps the Reticulum initialisation, path discovery, link
establishment, and packet handling exactly the same.
"""

import socket
import argparse
import sys
import time
import RNS
import requests

# --------------------------------------------------------------------
# 1.  Reticulum helper functions (almost identical to your MeshCurl)
# --------------------------------------------------------------------
def init_reticulum():
    """Initialise Reticulum once – this is a global singleton."""
    if not hasattr(init_reticulum, "ret"):
        init_reticulum.ret = RNS.Reticulum()
    return init_reticulum.ret


def get_identity():
    """Return a fresh Identity for this proxy instance."""
    return RNS.Identity()


def request_http(destination_hash, method, path, verbose=False):
    """
    Send an HTTP request over Reticulum and return the raw response bytes.
    Raises RuntimeError on failure.
    """
    # ----------------------------------------------------------------
    # 1.  Resolve destination hash to bytes
    # ----------------------------------------------------------------
    try:
        dest_hash = bytes.fromhex(destination_hash)
    except ValueError:
        raise RuntimeError(f"Invalid destination hash: {destination_hash}")

    # ----------------------------------------------------------------
    # 2.  Path discovery
    # ----------------------------------------------------------------
    if not RNS.Transport.has_path(dest_hash):
        RNS.Transport.request_path(dest_hash)
        for _ in range(10):
            time.sleep(1)
            if RNS.Transport.has_path(dest_hash):
                break
        else:
            raise RuntimeError("Could not find path to destination")

    # ----------------------------------------------------------------
    # 3.  Build the link
    # ----------------------------------------------------------------
    server_identity = RNS.Identity.recall(dest_hash)
    if server_identity is None:
        raise RuntimeError("Could not recall server identity")

    server_destination = RNS.Destination(
        server_identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        "rserver", "web"
    )
    link = RNS.Link(server_destination)

    # Wait for link to become active
    for _ in range(30):
        time.sleep(1)
        if link.status == RNS.Link.ACTIVE:
            break
    else:
        raise RuntimeError("Link establishment failed")

    # ----------------------------------------------------------------
    # 4.  Prepare callbacks to collect the reply
    # ----------------------------------------------------------------
    reply_container = {"data": None, "status": None}

    def resource_concluded(resource):
        if resource.status == RNS.Resource.COMPLETE:
            reply_container["data"] = resource.data.read()
        else:
            reply_container["status"] = resource.status

    link.set_resource_strategy(RNS.Link.ACCEPT_ALL)
    link.set_resource_concluded_callback(resource_concluded)

    # ----------------------------------------------------------------
    # 5.  Send the HTTP request packet
    # ----------------------------------------------------------------
    http_request = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: {destination_hash}\r\n"
        f"User-Agent: MeshProxy/1.0\r\n"
        f"Accept: text/html,*/*\r\n"
        f"\r\n"
    )
    packet = RNS.Packet(link, http_request.encode("utf-8"))
    packet.send()

    # ----------------------------------------------------------------
    # 6.  Wait for the reply (max 120 s)
    # ----------------------------------------------------------------
    for _ in range(240):
        time.sleep(0.5)
        if reply_container["data"] is not None:
            break
    else:
        raise RuntimeError("No response received")

    # ----------------------------------------------------------------
    # 7.  Clean up
    # ----------------------------------------------------------------
    link.teardown()
    return reply_container["data"]


# --------------------------------------------------------------------
# 3.  Simple TCP server that speaks HTTP
# --------------------------------------------------------------------
def handle_client(conn, addr, verbose=False):
    """
    Read a single HTTP request from the browser, forward it over
    Reticulum, and stream the reply back.
    """
    try:
        request = conn.recv(8192).decode("utf-8", errors="replace")
        if not request:
            return

        if verbose:
            print(f"\n=== Received request from {addr} ===")
            print(request)

        # Parse the request line
        lines = request.splitlines()
        if not lines:
            return
        method, url, _ = lines[0].split()
        method = method.upper()
        if method not in ("GET", "HEAD"):
            conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return

        # The browser will send the full URL when the proxy is used.
        # It looks like: GET http://<hash>/path HTTP/1.1
        # Extract <hash> and /path
        if url.startswith("http://"):
            rest = url[7:]            # strip http://
            dest_hash, _, path = rest.partition("/")
            path = "/" + path         # preserve leading slash
        else:
            # If the browser uses CONNECT or other, just bail
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return

        if verbose:
            print(f"Destination hash: {dest_hash}")
            print(f"Path: {path}")
        # ------------------------------------------------------------------
        # 4.  Validate the destination hash
        # ------------------------------------------------------------------
        # A valid Reticulum hash is 20 bytes → 40 hex digits.
        if  len(dest_hash) != 32:
            # The hash is not valid – redirect the client straight to the
            # original URL (i.e. the “internet”).
            if verbose:
                print(f"Invalid hash '{dest_hash}'. Redirecting to {url}")
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")

            try:        
                headers = {  
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",  
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",  
                    "Accept-Language": "en-US,en;q=0.5",  
                    "Referer": "https://www.google.com/"  # Mimic coming from a search engine  
                }  
 
                redirect = requests.get(url,headers=headers)
                content = redirect.content
                conn.sendall(content)            
            except RuntimeError as e:
                print(f'bad link {e}')
                
            return
        # ----------------------------------------------------------------
        # Forward over Reticulum
        # ----------------------------------------------------------------
        try:
            reply_bytes = request_http(dest_hash, method, path, verbose)
        except RuntimeError as e:
            if verbose:
                print(f"Error: {e}")
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return

        # ----------------------------------------------------------------
        # Build a minimal HTTP/1.1 response
        # ----------------------------------------------------------------
        status_line = "HTTP/1.1 200 OK\r\n"
        #status_line = "200 OK\r\n"
        headers = [
            "Content-Type: text/html; charset=utf-8",
            f"Content-Length: {len(reply_bytes)}",
            "Connection: close",
        ]
        response = (status_line + "\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + reply_bytes
#        print(reply_bytes.decode("utf-8"))
        conn.sendall(reply_bytes)

        if verbose:
            print(f"Sent {len(response)} bytes back to browser")

    finally:
        conn.close()


def run_server(port=8080, verbose=False):
    """Start the proxy listening on the given TCP port."""
    init_reticulum()
    print(f"MeshProxy listening on 127.0.0.1:{port} (GET/HEAD only)")
    if verbose:
        print("Press Ctrl‑C to stop")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(5)

        try:
            while True:
                conn, addr = srv.accept()
                # Handle each client in a new thread (or you can use async)
                import threading
                threading.Thread(target=handle_client, args=(conn, addr, verbose), daemon=True).start()
        except KeyboardInterrupt:
            print("\nShutting down")


# --------------------------------------------------------------------
# 4.  CLI entry point
# --------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MeshProxy – HTTP → Reticulum proxy")
    parser.add_argument("--port", type=int, default=8080, help="TCP port to listen on (default 8080)")
    parser.add_argument("--verbose", action="store_true", help="Print debug info")
    args = parser.parse_args()

    run_server(port=args.port, verbose=args.verbose)

