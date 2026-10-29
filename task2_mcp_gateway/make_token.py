"""Mint a demo token for the gateway: python -m task2_mcp_gateway.make_token viewer"""

import os
import sys
import time

import jwt

role = sys.argv[1] if len(sys.argv) > 1 else "viewer"
claims = {"sub": f"demo-{role}", "role": role, "exp": int(time.time()) + 3600}
print(jwt.encode(claims, os.environ["GATEWAY_JWT_SECRET"], algorithm="HS256"))
