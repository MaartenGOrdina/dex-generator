#!/usr/bin/env python3

import os
import sys
import time
import grpc
import gitlab
from dotenv import load_dotenv
from dex.api.v2 import api_pb2, api_pb2_grpc

# Load environment variables from .env file
load_dotenv()


def setup_dex_client(dex_host, dex_cert_path=None):
    """Set up gRPC connection to Dex server"""
    if dex_cert_path:
        # Load custom CA certificate for TLS
        with open(dex_cert_path, 'rb') as f:
            ca_cert = f.read()

        credentials = grpc.ssl_channel_credentials(root_certificates=ca_cert)

        # Create secure gRPC channel
        channel = grpc.secure_channel(dex_host, credentials)
    else:
        # Use default system certificates
        credentials = grpc.ssl_channel_credentials()
        channel = grpc.secure_channel(dex_host, credentials)

    return api_pb2_grpc.DexStub(channel)


def get_open_merge_requests(gitlab_url, gitlab_token, project_id, gitlab_cert_path=None):
    """Retrieve all open merge requests from GitLab"""
    # Configure SSL certificate verification
    if gitlab_cert_path:
        # Use custom certificate
        gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token, ssl_verify=gitlab_cert_path)
    else:
        # Use default certificate verification
        gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token)

    # Get the project
    project = gl.projects.get(project_id)

    # Get open merge requests
    mrs = project.mergerequests.list(state='opened', get_all=True)

    return mrs


def client_with_id_exists(dex_client, client_id):
    """Check if a client with the given ID already exists"""
    try:
        # List all existing clients
        response = dex_client.ListClients(api_pb2.ListClientsReq())

        # Check if any client has the matching ID
        for client in response.clients:
            if client.id == client_id:
                return True, client

        return False, None
    except grpc.RpcError as e:
        print(f"Failed to list clients: {e}")
        return False, None


def delete_client(dex_client, client_id):
    """Delete a Dex client by ID"""
    try:
        request = api_pb2.DeleteClientReq(id=client_id)
        dex_client.DeleteClient(request)
        print(f"🗑️  Deleted client: {client_id}")
        return True
    except grpc.RpcError as e:
        print(f"Failed to delete client {client_id}: {e}")
        return False


def generate_client_secret(mr_number):
    """Generate client secret (use proper secret generation in production)"""
    # In production, use secrets module or similar for secure random generation
    import secrets
    return secrets.token_urlsafe(32)


def process_and_create_client(dex_client, mr):
    """Process a merge request and create a Dex client for it"""
    # Generate client configuration based on MR
    client_id = f"mr-{mr.iid}"
    client_secret = generate_client_secret(mr.iid)
    redirect_uri = f"https://mr-{mr.iid}.preview.example.com/callback"

    # Check if client already exists
    exists, existing_client = client_with_id_exists(dex_client, client_id)

    if exists:
        print(f"Client for MR !{mr.iid} already exists (ID: {existing_client.id}). Skipping.")
        return False

    # Create new client
    new_client = api_pb2.Client(
        id=client_id,
        secret=client_secret,
        redirect_uris=[redirect_uri],
        trusted_peers=[],
        public=False,
        name=f"MR !{mr.iid} - {mr.title}",
        logo_url=""
    )

    try:
        request = api_pb2.CreateClientReq(client=new_client)
        response = dex_client.CreateClient(request)

        print(f"✓ Created client for MR !{mr.iid}: {response.client.name} (ID: {response.client.id})")
        print(f"  Redirect URI: {redirect_uri}")
        return True
    except grpc.RpcError as e:
        print(f"Failed to create client for MR !{mr.iid}: {e}")
        return False


def process_merge_requests(gitlab_url, gitlab_token, project_id, dex_client, known_mr_ids, gitlab_cert_path=None):
    """Process all open merge requests and track which ones we've seen"""
    try:
        # Get open merge requests from GitLab
        mrs = get_open_merge_requests(gitlab_url, gitlab_token, project_id, gitlab_cert_path)

        current_mr_ids = set()
        new_mrs_found = False

        # Process each MR
        for mr in mrs:
            current_mr_ids.add(mr.iid)

            # Check if this is a new MR we haven't seen before
            if mr.iid not in known_mr_ids:
                print(f"\n🆕 New MR detected: !{mr.iid} - {mr.title}")
                created = process_and_create_client(dex_client, mr)
                if created:
                    new_mrs_found = True

        # Check for closed/merged MRs and delete their clients
        closed_mrs = known_mr_ids - current_mr_ids
        if closed_mrs:
            print(f"\n📋 MRs no longer open: {', '.join(f'!{iid}' for iid in closed_mrs)}")
            for mr_iid in closed_mrs:
                client_id = f"mr-{mr_iid}"
                delete_client(dex_client, client_id)

        return current_mr_ids, new_mrs_found

    except gitlab.exceptions.GitlabError as e:
        print(f"GitLab API error: {e}")
        return known_mr_ids, False
    except Exception as e:
        print(f"Error processing merge requests: {e}")
        return known_mr_ids, False


def main():
    # GitLab configuration from environment variables
    gitlab_token = os.getenv('GITLAB_TOKEN')
    gitlab_url = os.getenv('GITLAB_URL')  # e.g., "https://gitlab.com"
    project_id = os.getenv('GITLAB_PROJECT_ID')  # e.g., "123" or "group/project"
    gitlab_cert = os.getenv('GITLAB_CERT_PATH')  # Optional: path to GitLab CA certificate

    # Dex configuration from environment variables
    dex_host = os.getenv('DEX_HOST')  # Dex gRPC endpoint
    dex_cert = os.getenv('DEX_CERT_PATH')  # Optional: path to Dex CA certificate

    check_interval = int(os.getenv('CHECK_INTERVAL', '30'))  # Default 30 seconds

    if not all([gitlab_token, gitlab_url, project_id, dex_host]):
        print("Error: GITLAB_TOKEN, GITLAB_URL, GITLAB_PROJECT_ID, and DEX_HOST environment variables must be set")
        sys.exit(1)

    print(f"Starting MR monitor for project: {project_id}")
    print(f"Dex gRPC host: {dex_host}")
    print(f"Checking for new MRs every {check_interval} seconds")
    if gitlab_cert:
        print(f"Using GitLab certificate: {gitlab_cert}")
    if dex_cert:
        print(f"Using Dex certificate: {dex_cert}")
    print("Press Ctrl+C to stop\n")

    # Set up Dex gRPC client
    dex_client = setup_dex_client(dex_host, dex_cert)

    # Track which MR IDs we've already seen
    known_mr_ids = set()

    # Initial run
    print("Performing initial check...")
    known_mr_ids, _ = process_merge_requests(gitlab_url, gitlab_token, project_id, dex_client, known_mr_ids,
                                             gitlab_cert)
    print(f"\nMonitoring {len(known_mr_ids)} open MR(s)")

    # Continuous monitoring loop
    try:
        while True:
            time.sleep(check_interval)

            print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Checking for new MRs...")
            known_mr_ids, new_mrs = process_merge_requests(gitlab_url, gitlab_token, project_id, dex_client,
                                                           known_mr_ids, gitlab_cert)

            if not new_mrs:
                print(f"No new MRs. Currently monitoring {len(known_mr_ids)} open MR(s)")

    except KeyboardInterrupt:
        print("\n\nStopping MR monitor. Goodbye!")
        sys.exit(0)


if __name__ == "__main__":
    main()