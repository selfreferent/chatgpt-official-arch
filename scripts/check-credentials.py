#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Verify checkout credentials are accessible to this user without printing them."""
import subprocess
import sys

result=subprocess.run(['git','config','--get-regexp',r'^http\..*\.extraheader$'],capture_output=True,text=True)
if result.returncode not in (0,1):
    raise SystemExit('Could not read checkout credential configuration')
present=any('AUTHORIZATION:' in line.upper() for line in result.stdout.splitlines())
if present != (sys.argv[1]=='present'):
    raise SystemExit('Checkout credential accessibility did not match the required boundary')
subprocess.run(['git','ls-remote','--exit-code','origin','HEAD'],stdout=subprocess.DEVNULL,check=True)
print('Checkout credential boundary verified for the unprivileged builder')
