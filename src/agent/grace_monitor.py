#!/usr/bin/env python3
"""Backward compatibility forwarding script for grace_monitor.py."""
import sys
import runpy

if __name__ == "__main__":
    runpy.run_module("agent.observability.grace_monitor", run_name="__main__")
