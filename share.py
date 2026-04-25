"""
share.py — expose EmulAItor to the internet via ngrok.
Usage: python share.py

First time: sign up free at https://ngrok.com, grab your authtoken,
then run: python share.py --setup YOUR_TOKEN
"""

import argparse
import subprocess
import sys
import time

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", metavar="TOKEN", help="Save your ngrok authtoken")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    try:
        from pyngrok import ngrok, conf
    except ImportError:
        print("Run: pip install pyngrok")
        sys.exit(1)

    # Save token if provided
    if args.setup:
        ngrok.set_auth_token(args.setup)
        print(f"✓ Token saved.")

    # Check token is configured
    try:
        conf.get_default().auth_token
    except Exception:
        pass

    print(f"\nStarting tunnel to http://127.0.0.1:{args.port} ...\n")

    try:
        tunnel = ngrok.connect(args.port, "http")
        public_url = tunnel.public_url

        print("=" * 54)
        print(f"  Share this link with your friend:")
        print()
        print(f"  {public_url}")
        print()
        print("  Works from anywhere — no VPN, no setup needed.")
        print("  Keep this window open while they're using it.")
        print("=" * 54)
        print("\n  Press Ctrl+C to stop sharing.\n")

        # Keep alive
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nTunnel closed.")
            ngrok.disconnect(public_url)
            ngrok.kill()

    except Exception as exc:
        err = str(exc)
        if "authtoken" in err.lower() or "auth" in err.lower() or "401" in err:
            print("⚠  ngrok needs a free account token.")
            print()
            print("  1. Sign up free at: https://ngrok.com")
            print("  2. Copy your authtoken from: https://dashboard.ngrok.com/get-started/your-authtoken")
            print("  3. Run: python share.py --setup YOUR_TOKEN_HERE")
            print("  4. Then run: python share.py")
        else:
            print(f"Error: {exc}")
        sys.exit(1)

if __name__ == "__main__":
    main()
