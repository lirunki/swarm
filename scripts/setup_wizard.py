#!/usr/bin/env python3
"""Checkout entry point after setup_swarm.sh has provisioned its selected runtime."""

from swarm.setup_wizard import main

if __name__ == "__main__":
    raise SystemExit(main())
